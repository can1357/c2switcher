"""Modern session analytics report with polished visuals."""

from __future__ import annotations

import sqlite3
import webbrowser
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, Tuple

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()
_WORKTREE_REPO_CACHE: Dict[Tuple[Path, str], str] = {}


def _resolve_worktree_repo(base_dir: Path, worktree_name: str) -> str:
    segments = worktree_name.split("-")
    for length in range(len(segments), 0, -1):
        candidate = "-".join(segments[:length])
        if (base_dir / candidate).is_dir():
            return candidate
    return segments[0] if segments else worktree_name


def extract_project(path: Optional[str]) -> str:
    if not path:
        return "unknown"

    p = Path(path)
    parts = p.parts

    # Collapse git worktree folders back to repo names
    try:
        wt_idx = parts.index(".worktrees")
    except ValueError:
        pass
    else:
        worktree_name = parts[wt_idx + 1] if len(parts) > wt_idx + 1 else ""
        base_dir = Path(*parts[:wt_idx]) if wt_idx > 0 else Path("/")
        if worktree_name:
            key = (base_dir, worktree_name)
            repo_name = _WORKTREE_REPO_CACHE.get(key)
            if repo_name is None:
                repo_name = _resolve_worktree_repo(base_dir, worktree_name)
                _WORKTREE_REPO_CACHE[key] = repo_name
            return repo_name or worktree_name

    if "Projects" in parts:
        idx = parts.index("Projects")
        if idx + 1 < len(parts):
            return parts[idx + 1]

    last = p.name
    return last if last else "unknown"


def load_sessions(db_path: Path, min_duration_sec: int, days: int) -> pd.DataFrame:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    query = """
        SELECT
            s.session_id,
            s.account_uuid,
            s.cwd,
            s.created_at,
            s.ended_at,
            a.nickname,
            a.display_name,
            a.email
        FROM sessions s
        LEFT JOIN accounts a ON s.account_uuid = a.uuid
        WHERE s.ended_at IS NOT NULL
        ORDER BY s.created_at DESC;
    """
    df = pd.read_sql_query(query, conn)
    conn.close()

    if df.empty:
        return df

    # Convert UTC timestamps to local timezone for accurate hour/weekday analysis
    local_tz = datetime.now().astimezone().tzinfo
    df["created_at"] = pd.to_datetime(df["created_at"], utc=True).dt.tz_convert(local_tz).dt.tz_localize(None)
    df["ended_at"] = pd.to_datetime(df["ended_at"], utc=True).dt.tz_convert(local_tz).dt.tz_localize(None)
    df["duration_min"] = (df["ended_at"] - df["created_at"]).dt.total_seconds() / 60
    df = df[df["duration_min"] >= min_duration_sec / 60]

    if days > 0:
        cutoff = datetime.now() - timedelta(days=days)
        df = df[df["created_at"] >= cutoff]

    df["project"] = df["cwd"].apply(extract_project)
    df["account"] = df["nickname"].fillna(df["display_name"]).fillna("unknown")
    df["date"] = df["created_at"].dt.date
    df["hour"] = df["created_at"].dt.hour
    df["weekday"] = df["created_at"].dt.day_name()

    return df


def summarize_high_level(df: pd.DataFrame) -> Panel:
    total_sessions = len(df)
    total_hours = df["duration_min"].sum() / 60
    unique_projects = df["project"].nunique()
    unique_accounts = df["account"].nunique()
    span_days = (df["created_at"].max().date() - df["created_at"].min().date()).days + 1
    avg_daily_hours = total_hours / span_days if span_days > 0 else 0
    median_session = df["duration_min"].median()
    longest = df.nlargest(1, "duration_min").iloc[0]
    longest_desc = f"{longest['project']} · {longest['duration_min']:.0f} min ({longest['account']})"

    body = (
        f"[bold]{total_sessions}[/] sessions · "
        f"[bold]{total_hours:.1f}[/] hrs total · "
        f"{unique_projects} projects · "
        f"{unique_accounts} accounts\n"
        f"Span: {span_days} days · Avg day: {avg_daily_hours:.1f}h · "
        f"Median session: {median_session:.0f} min\n"
        f"Longest session: {longest_desc}"
    )
    return Panel(body, title="Session Overview", border_style="cyan", box=box.ROUNDED)


def top_projects_table(df: pd.DataFrame) -> Table:
    top_projects = (
        df.groupby("project")
        .agg(
            hours=("duration_min", lambda s: s.sum() / 60),
            share=("duration_min", lambda s: s.sum() / df["duration_min"].sum()),
            sessions=("session_id", "count"),
            median_min=("duration_min", "median"),
            avg_min=("duration_min", "mean"),
        )
        .sort_values("hours", ascending=False)
        .head(10)
    )

    table = Table(
        title="Top Focus Areas",
        box=box.MINIMAL_DOUBLE_HEAD,
        pad_edge=False,
        show_lines=False,
    )
    table.add_column("Project", justify="left", style="bold")
    table.add_column("Hours", justify="right")
    table.add_column("Share", justify="right")
    table.add_column("Sessions", justify="right")
    table.add_column("Median", justify="right")
    table.add_column("Avg", justify="right")

    for idx, row in top_projects.iterrows():
        table.add_row(
            idx,
            f"{row['hours']:.1f}",
            f"{row['share'] * 100:5.1f}%",
            f"{int(row['sessions'])}",
            f"{row['median_min']:.0f}m",
            f"{row['avg_min']:.0f}m",
        )
    return table


def productive_hours_table(df: pd.DataFrame) -> Table:
    hour_stats = (
        df.groupby("hour")["duration_min"].sum().reindex(range(24), fill_value=0).sort_values(ascending=False)
    )
    top_hours = hour_stats.head(6)
    table = Table(
        title="Peak Focus Windows",
        box=box.SIMPLE_HEAVY,
        pad_edge=False,
    )
    table.add_column("Hour", justify="center")
    table.add_column("Total Time", justify="right")
    table.add_column("Sessions", justify="right")
    table.add_column("Signature Project", justify="left")

    for hour, minutes in top_hours.items():
        sessions = len(df[df["hour"] == hour])
        project = (
            df[df["hour"] == hour]
            .groupby("project")["duration_min"]
            .sum()
            .sort_values(ascending=False)
            .head(1)
        )
        project_name = project.index[0] if not project.empty else "—"
        table.add_row(
            f"{hour:02d}:00",
            f"{minutes/60:.1f}h",
            str(sessions),
            project_name,
        )
    return table


def account_mix_table(df: pd.DataFrame) -> Table:
    account_stats = (
        df.groupby("account")
        .agg(
            hours=("duration_min", lambda s: s.sum() / 60),
            sessions=("session_id", "count"),
            unique_projects=("project", "nunique"),
        )
        .sort_values("hours", ascending=False)
    )

    table = Table(
        title="Account Distribution",
        box=box.SQUARE,
        pad_edge=False,
    )
    table.add_column("Account", style="bold")
    table.add_column("Hours", justify="right")
    table.add_column("Sessions", justify="right")
    table.add_column("Projects", justify="right")
    table.add_column("Longest Session", justify="right")

    for acc, row in account_stats.iterrows():
        longest = df[df["account"] == acc]["duration_min"].max()
        table.add_row(
            acc,
            f"{row['hours']:.1f}",
            str(int(row["sessions"])),
            str(int(row["unique_projects"])),
            f"{longest:.0f}m",
        )
    return table


def recent_context_panel(df: pd.DataFrame) -> Panel:
    window = df["created_at"].max() - timedelta(days=7)
    last_week = df[df["created_at"] >= window]
    if last_week.empty:
        body = "No sessions recorded in the last 7 days."
    else:
        hours = last_week["duration_min"].sum() / 60
        sessions = last_week["session_id"].nunique()
        projects = last_week["project"].value_counts()
        top_projects = (projects / projects.sum() * 100).round().astype(int).head(3)
        project_summary = ", ".join(
            f"[bold]{name}[/] ({pct}%)" for name, pct in top_projects.items()
        )
        body = (
            f"Last 7 days: [bold]{hours:.1f}[/] hrs across {sessions} sessions\n"
            f"Top projects: {project_summary}"
        )
    return Panel(body, title="Recent Momentum", border_style="magenta", box=box.ROUNDED)


def recommend_actions(df: pd.DataFrame) -> Panel:
    latest_sessions = df.sort_values("created_at", ascending=False).head(5)
    lines = []
    if not latest_sessions.empty:
        recent_projects = Counter(latest_sessions["project"])
        fav = recent_projects.most_common(1)[0][0]
        lines.append(
            f"Maintain momentum on [bold]{fav}[/] — it featured in {recent_projects[fav]} "
            "of your last 5 sessions."
        )

    busiest_hour = df.groupby("hour")["duration_min"].sum().idxmax()
    lines.append(f"Your peak focus hour is [bold]{busiest_hour:02d}:00[/]; consider protecting that time window.")

    if df["duration_min"].mean() < 45:
        lines.append("Average session length is under 45 minutes; batching tasks might reduce context-switching.")

    if df["project"].nunique() > 8:
        lines.append("High project count detected — consider archiving or batching related projects to keep focus.")

    body = "\n".join(f"- {line}" for line in lines) if lines else "No strong action items detected."
    return Panel(body, title="Quick Recommendations", border_style="yellow", box=box.ROUNDED)


def create_visualizations(df: pd.DataFrame, output_path: Path, days: int, show: bool) -> None:
    plt.style.use("seaborn-v0_8")

    fig = plt.figure(figsize=(18, 12))
    fig.suptitle("C2Switcher Session Insights", fontsize=18, fontweight="bold")
    gs = fig.add_gridspec(2, 2, height_ratios=[1, 1.1], wspace=0.25, hspace=0.3)

    # Panel 1: Daily hours trend
    ax1 = fig.add_subplot(gs[0, 0])
    daily = df.groupby("date")["duration_min"].sum() / 60
    if days > 0:
        daily = daily.tail(days)
    rolling = daily.rolling(window=7, min_periods=1).mean()
    ax1.plot(daily.index, daily.values, marker="o", linewidth=2, label="Daily hours", color="#2b8a3e")
    ax1.plot(rolling.index, rolling.values, linestyle="--", linewidth=2, label="7-day avg", color="#1971c2")
    ax1.fill_between(daily.index, daily.values, color="#2b8a3e", alpha=0.2)
    ax1.set_xlabel("Date")
    ax1.set_ylabel("Hours")
    ax1.set_title("Daily Focus Time")
    ax1.grid(alpha=0.3)
    ax1.legend(frameon=False)
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    plt.setp(ax1.get_xticklabels(), rotation=45, ha="right")

    # Panel 2: Top projects bar
    ax2 = fig.add_subplot(gs[0, 1])
    project_hours = df.groupby("project")["duration_min"].sum().sort_values(ascending=False) / 60
    top_projects = project_hours.head(8)[::-1]
    bars = ax2.barh(top_projects.index, top_projects.values, color="#f08c00")
    ax2.set_xlabel("Total hours")
    ax2.set_title("Top Projects")
    for bar, value in zip(bars, top_projects.values):
        ax2.text(value + 0.1, bar.get_y() + bar.get_height() / 2, f"{value:.1f}h", va="center")

    # Panel 3: Heatmap hour vs weekday
    ax3 = fig.add_subplot(gs[1, 0])
    weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    pivot = (
        df.assign(weekday=pd.Categorical(df["weekday"], categories=weekdays, ordered=True))
        .pivot_table(index="weekday", columns="hour", values="duration_min", aggfunc="sum", fill_value=0)
        .reindex(weekdays)
        .fillna(0)
    )
    data = pivot.values
    im = ax3.imshow(data, aspect="auto", cmap="YlGnBu")
    ax3.set_title("Energy by Weekday & Hour")
    ax3.set_xlabel("Hour of day")
    ax3.set_ylabel("Weekday")
    ax3.set_xticks(range(0, 24, 2))
    ax3.set_xticklabels([f"{h:02d}" for h in range(0, 24, 2)])
    ax3.set_yticks(range(len(pivot.index)))
    ax3.set_yticklabels(pivot.index)
    cbar = plt.colorbar(im, ax=ax3, shrink=0.8)
    cbar.set_label("Minutes")

    # Panel 4: Session length distribution
    ax4 = fig.add_subplot(gs[1, 1])
    durations = df["duration_min"]
    bins = np.linspace(0, min(240, durations.max() + 10), 30)
    ax4.hist(durations, bins=bins, color="#748ffc", edgecolor="white", alpha=0.9)
    ax4.axvline(durations.median(), color="#e03131", linestyle="--", linewidth=2, label="Median")
    ax4.axvline(durations.mean(), color="#2f9e44", linestyle=":", linewidth=2, label="Mean")
    ax4.set_xlabel("Session length (minutes)")
    ax4.set_ylabel("Count")
    ax4.set_title("Session Duration Distribution")
    ax4.legend(frameon=False)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=200, bbox_inches="tight")
    console.print(f"[bold green]✓[/] Saved visualization to [link=file://{output_path}]{output_path}[/]")

    if output_path.exists():
        console.print(f"[dim]Figure size: {output_path.stat().st_size / 1024:.1f} KiB[/]")

    webbrowser.open(f"file://{output_path.absolute()}")

    if show:
        plt.show()
    else:
        plt.close(fig)


def generate_session_report(
    db_path: Path,
    output_path: Path,
    days: int = 30,
    min_duration: int = 60,
    show: bool = False,
) -> None:
    if not db_path.exists():
        console.print(f"[bold red]Database not found:[/] {db_path}")
        return

    console.print(f"[bold cyan]Loading sessions from[/] {db_path}")
    df = load_sessions(db_path, min_duration, days)

    if df.empty:
        console.print("[yellow]No sessions found that match the filters.[/]")
        return

    console.print(summarize_high_level(df))
    console.print()
    console.print(top_projects_table(df))
    console.print()
    console.print(productive_hours_table(df))
    console.print()
    console.print(account_mix_table(df))
    console.print()
    console.print(recent_context_panel(df))
    console.print()
    console.print(recommend_actions(df))

    console.print("\n[bold cyan]Generating visualization…[/]")
    create_visualizations(df, output_path, days, show)

