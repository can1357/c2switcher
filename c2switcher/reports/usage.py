"""Modern usage analytics report with visual forecasts."""

from __future__ import annotations

import math
import sqlite3
import webbrowser
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.lines import Line2D
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()


def load_usage_history(db_path: Path) -> pd.DataFrame:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    query = """
        SELECT
            uh.id,
            uh.account_uuid,
            uh.queried_at,
            uh.five_hour_utilization,
            uh.five_hour_resets_at,
            uh.seven_day_utilization,
            uh.seven_day_resets_at,
            uh.seven_day_opus_utilization,
            uh.seven_day_opus_resets_at,
            a.nickname,
            a.display_name,
            a.email
        FROM usage_history uh
        LEFT JOIN accounts a ON uh.account_uuid = a.uuid
        ORDER BY uh.queried_at ASC;
    """

    df = pd.read_sql_query(query, conn)
    conn.close()

    if df.empty:
        return df

    time_cols = [
        "queried_at",
        "five_hour_resets_at",
        "seven_day_resets_at",
        "seven_day_opus_resets_at",
    ]
    datetime_updates = {
        col: pd.to_datetime(df[col], utc=True, errors="coerce").dt.tz_localize(None)
        for col in time_cols
    }
    df = df.assign(**datetime_updates)

    df = df.assign(account=df["nickname"].fillna(df["display_name"]).fillna("unknown"))

    numeric_cols = [
        "five_hour_utilization",
        "seven_day_utilization",
        "seven_day_opus_utilization",
    ]
    numeric_updates = {col: pd.to_numeric(df[col], errors="coerce") for col in numeric_cols}
    df = df.assign(**numeric_updates)

    return df


def slope_per_hour(series: pd.Series, times: pd.Series) -> float:
    if len(series) < 2:
        return 0.0
    elapsed = (times - times.iloc[0]).dt.total_seconds() / 3600
    if (elapsed == 0).all():
        return 0.0
    slope, _ = np.polyfit(elapsed, series, 1)
    return max(0.0, slope)


def format_horizon(hours: Optional[float]) -> str:
    if hours is None or hours == float("inf"):
        return "—"
    if hours >= 48:
        return f"{hours / 24:.1f}d"
    if hours >= 1:
        return f"{hours:.1f}h"
    return f"{hours * 60:.0f}m"


@dataclass
class AccountForecast:
    account: str
    latest_timestamp: datetime
    current_7d: float
    current_opus: float
    current_5h: float
    rate_7d: float
    rate_opus: float
    hours_to_cap_7d: float
    hours_to_cap_opus: float
    hours_until_7d_reset: Optional[float]
    hours_until_opus_reset: Optional[float]
    hours_until_5h_reset: Optional[float]
    hits_7d_before_reset: bool
    hits_opus_before_reset: bool
    first_limit_type: Optional[str]
    first_limit_hours: float
    status: str
    headline: str
    reset_7d_at: Optional[datetime]
    reset_opus_at: Optional[datetime]


def forecast_account(acc_df: pd.DataFrame, window_hours: int) -> Optional[AccountForecast]:
    if acc_df.empty:
        return None

    latest = acc_df.iloc[-1]
    now = latest["queried_at"]

    window_start = now - timedelta(hours=window_hours)
    recent = acc_df[acc_df["queried_at"] >= window_start]
    if len(recent) < 2:
        recent = acc_df.tail(5)

    rate_7d = slope_per_hour(recent["seven_day_utilization"], recent["queried_at"])
    rate_opus = slope_per_hour(recent["seven_day_opus_utilization"], recent["queried_at"])

    current_7d = float(latest["seven_day_utilization"] or 0)
    current_opus = float(latest["seven_day_opus_utilization"] or 0)
    current_5h = float(latest["five_hour_utilization"] or 0)

    def hours_until(reset_time: Optional[datetime]) -> Optional[float]:
        if pd.isna(reset_time):
            return None
        delta = (reset_time - now).total_seconds() / 3600
        return max(0.0, delta)

    reset_7d_at = latest["seven_day_resets_at"]
    reset_opus_at = latest["seven_day_opus_resets_at"]

    hours_until_7d_reset = hours_until(reset_7d_at)
    hours_until_opus_reset = hours_until(reset_opus_at)
    hours_until_5h_reset = hours_until(latest["five_hour_resets_at"])

    def hours_to_cap(current: float, rate: float) -> float:
        if rate <= 0:
            return float("inf")
        return max(0.0, (100 - current) / rate)

    hours_to_cap_7d = hours_to_cap(current_7d, rate_7d)
    hours_to_cap_opus = hours_to_cap(current_opus, rate_opus)

    hits_7d_before_reset = hours_to_cap_7d != float("inf") and (
        hours_until_7d_reset is None or hours_to_cap_7d < hours_until_7d_reset
    )
    hits_opus_before_reset = hours_to_cap_opus != float("inf") and (
        hours_until_opus_reset is None or hours_to_cap_opus < hours_until_opus_reset
    )

    limit_candidates = []
    if hits_7d_before_reset:
        limit_candidates.append(("7-day overall", hours_to_cap_7d))
    if hits_opus_before_reset:
        limit_candidates.append(("7-day Opus", hours_to_cap_opus))

    if limit_candidates:
        first_limit_type, first_limit_hours = min(limit_candidates, key=lambda item: item[1])
    else:
        first_limit_type, first_limit_hours = (None, float("inf"))

    if first_limit_type is None:
        status = "🟢 Reset"
        resets = [val for val in (hours_until_7d_reset, hours_until_opus_reset) if val is not None]
        if resets:
            soonest_reset = min(resets)
            headline = f"Resets in {format_horizon(soonest_reset)} before limits"
        else:
            headline = "Usage steady; no limits projected"
    else:
        if first_limit_hours < 6:
            status = "🔴 Critical"
        elif first_limit_hours < 24:
            status = "🟡 Watch"
        else:
            status = "🟢 OK"
        headline = f"{first_limit_type} limit in {format_horizon(first_limit_hours)}"

    return AccountForecast(
        account=latest["account"],
        latest_timestamp=now,
        current_7d=current_7d,
        current_opus=current_opus,
        current_5h=current_5h,
        rate_7d=rate_7d,
        rate_opus=rate_opus,
        hours_to_cap_7d=hours_to_cap_7d,
        hours_to_cap_opus=hours_to_cap_opus,
        hours_until_7d_reset=hours_until_7d_reset,
        hours_until_opus_reset=hours_until_opus_reset,
        hours_until_5h_reset=hours_until_5h_reset,
        hits_7d_before_reset=hits_7d_before_reset,
        hits_opus_before_reset=hits_opus_before_reset,
        first_limit_type=first_limit_type,
        first_limit_hours=first_limit_hours,
        status=status,
        headline=headline,
        reset_7d_at=reset_7d_at,
        reset_opus_at=reset_opus_at,
    )


def overall_status_panel(forecasts: Iterable[AccountForecast]) -> Panel:
    forecasts = list(forecasts)
    safe = sum(1 for f in forecasts if f.status.startswith("🟢"))
    warning = sum(1 for f in forecasts if f.status.startswith("🟡"))
    critical = sum(1 for f in forecasts if f.status.startswith("🔴"))

    if critical:
        color = "red"
        message = f"{critical} account(s) critical; rotate immediately."
    elif warning:
        color = "yellow"
        message = f"{warning} account(s) trending up; plan fallback."
    else:
        color = "green"
        message = f"All {safe} account(s) stable."

    limit_counts = Counter(f.first_limit_type for f in forecasts if f.first_limit_type)
    limit_summary_parts = []
    for label in ("7-day overall", "7-day Opus"):
        count = limit_counts.get(label)
        if count:
            limit_summary_parts.append(f"{count}× {label}")
    other_parts = [
        f"{count}× {label}"
        for label, count in limit_counts.items()
        if label not in {"7-day overall", "7-day Opus"}
    ]
    limit_summary_parts.extend(other_parts)
    limit_summary = ", ".join(limit_summary_parts)

    reset_candidates = []
    for f in forecasts:
        if f.hours_until_7d_reset is not None:
            reset_candidates.append(f.hours_until_7d_reset)
        if f.hours_until_opus_reset is not None:
            reset_candidates.append(f.hours_until_opus_reset)

    body_lines = [f"[bold]{message}[/]"]
    if limit_summary:
        body_lines.append(f"Upcoming limits: {limit_summary}")
    if reset_candidates:
        body_lines.append(f"Nearest reset in {format_horizon(min(reset_candidates))}.")

    body = "\n".join(body_lines)
    return Panel(body, title="Fleet Health", border_style=color, box=box.ROUNDED)


def accounts_table(forecasts: Iterable[AccountForecast]) -> Table:
    table = Table(
        title="Account Burn Forecast",
        box=box.MINIMAL_DOUBLE_HEAD,
        show_lines=False,
        pad_edge=False,
    )
    table.add_column("Account", style="bold")
    table.add_column("Status")
    table.add_column("7d", justify="right")
    table.add_column("Opus", justify="right")
    table.add_column("5h", justify="right")
    table.add_column("7d Rate", justify="right")
    table.add_column("Opus Rate", justify="right")
    table.add_column("First Limit", justify="left")
    table.add_column("7d Reset", justify="right")
    table.add_column("Opus Reset", justify="right")
    table.add_column("Action", justify="left")

    for f in forecasts:
        if f.first_limit_type:
            limit_eta = f"{f.first_limit_type} → {format_horizon(f.first_limit_hours)}"
        else:
            limit_eta = "Resets first"
        action = f.headline

        table.add_row(
            f.account,
            f.status,
            f"{f.current_7d:.0f}%",
            f"{f.current_opus:.0f}%",
            f"{f.current_5h:.0f}%",
            f"{f.rate_7d:.2f}%/h",
            f"{f.rate_opus:.2f}%/h",
            limit_eta,
            format_horizon(f.hours_until_7d_reset),
            format_horizon(f.hours_until_opus_reset),
            action,
        )
    return table


def quick_recos_panel(forecasts: Iterable[AccountForecast]) -> Panel:
    lines = []
    for f in forecasts:
        if f.first_limit_type:
            horizon = format_horizon(f.first_limit_hours)
            if f.first_limit_hours < 6:
                lines.append(f"[bold]{f.account}[/] hits {f.first_limit_type} in {horizon} — rotate now.")
            elif f.first_limit_hours < 24:
                lines.append(f"[bold]{f.account}[/] {f.first_limit_type} limit in {horizon} — prep fallback.")
            else:
                lines.append(f"[bold]{f.account}[/] trending toward {f.first_limit_type} in {horizon} — monitor.")
        elif f.rate_7d > 0.5 or f.rate_opus > 0.5:
            lines.append(f"[bold]{f.account}[/] rising quickly but resets first — keep an eye on burn rate.")
        if f.current_5h > 80:
            lines.append(f"[bold]{f.account}[/] five-hour window above 80%; give it a breather.")
    if not lines:
        lines = ["All accounts have comfortable margins. Keep monitoring every few hours."]
    body = "\n".join(f"- {line}" for line in lines)
    return Panel(body, title="Playbook", border_style="cyan", box=box.ROUNDED)


def fleet_capacity_panel(forecasts: Iterable[AccountForecast]) -> Panel:
    forecasts = list(forecasts)
    if not forecasts:
        return Panel(
            "[dim]No accounts available to assess capacity.[/]",
            title="Fleet Capacity",
            border_style="magenta",
            box=box.ROUNDED,
        )

    per_account_capacity = 100 / (7 * 24)

    total_rate_7d = sum(max(f.rate_7d, 0.0) for f in forecasts)
    total_rate_opus = sum(max(f.rate_opus, 0.0) for f in forecasts)

    def required_accounts(total_rate: float) -> int:
        if total_rate <= 0:
            return 0
        return max(1, math.ceil(total_rate / per_account_capacity))

    required_overall = required_accounts(total_rate_7d)
    required_opus = required_accounts(total_rate_opus)
    recommended_fleet = max(required_overall, required_opus)

    at_risk_accounts = [f for f in forecasts if f.hits_7d_before_reset or f.hits_opus_before_reset]
    projected_available = len(forecasts) - len(at_risk_accounts)
    projected_shortfall = max(0, recommended_fleet - projected_available)

    limit_horizons = [
        f.first_limit_hours
        for f in forecasts
        if f.first_limit_type and f.first_limit_hours != float("inf")
    ]
    soonest_limit = min(limit_horizons) if limit_horizons else None

    lines = [f"[bold]Accounts on roster:[/] {len(forecasts)}"]

    if total_rate_7d > 0:
        lines.append(f"7d Sonnet burn: {total_rate_7d:.2f}%/h → needs {required_overall} account(s)")
    else:
        lines.append("7d Sonnet burn: [dim]idle[/dim]")

    if total_rate_opus > 0:
        lines.append(f"7d Opus burn: {total_rate_opus:.2f}%/h → needs {required_opus} account(s)")
    else:
        lines.append("7d Opus burn: [dim]idle[/dim]")

    if recommended_fleet > 0:
        lines.append(f"[bold]Recommended fleet size:[/] {recommended_fleet} account(s)")

    if len(forecasts) >= recommended_fleet:
        headroom = len(forecasts) - recommended_fleet
        lines.append(f"Current headroom: {headroom} account(s)")
    else:
        lines.append(f"[red]Shortfall today:[/] add {recommended_fleet - len(forecasts)} account(s)")

    if at_risk_accounts:
        lines.append(
            f"Projected drop-offs: {len(at_risk_accounts)} account(s) hit limits before reset "
            f"({format_horizon(soonest_limit)} earliest)."
        )
        if projected_shortfall > 0:
            lines.append(f"[yellow]Action:[/] secure {projected_shortfall} additional account(s) soon.")
    else:
        lines.append("No accounts expected to cap before their resets.")

    body = "\n".join(lines)
    return Panel(body, title="Fleet Capacity", border_style="magenta", box=box.ROUNDED)


def create_visualizations(df: pd.DataFrame, forecasts: Iterable[AccountForecast], output_path: Path, show: bool) -> None:
    forecasts = list(forecasts)
    plt.style.use("seaborn-v0_8")

    fig = plt.figure(figsize=(18, 12))
    fig.suptitle("C2Switcher Usage Risk Dashboard", fontsize=18, fontweight="bold")
    gs = fig.add_gridspec(2, 2, height_ratios=[1.1, 1], wspace=0.25, hspace=0.3)

    accounts = [f.account for f in forecasts]
    forecast_map = {f.account: f for f in forecasts}

    # Panel 1: 7-day overall utilization trend
    ax1 = fig.add_subplot(gs[0, 0])
    for account in accounts:
        acc_data = df[df["account"] == account]
        if acc_data.empty:
            continue
        forecast = forecast_map.get(account)
        (line,) = ax1.plot(
            acc_data["queried_at"],
            acc_data["seven_day_utilization"],
            marker="o",
            linewidth=2,
            markersize=3,
            label=f"{account}",
        )
        color = line.get_color()

        if forecast:
            if forecast.reset_7d_at is not None and not pd.isna(forecast.reset_7d_at):
                ax1.axvline(forecast.reset_7d_at, color=color, linestyle=":", alpha=0.35)
            if forecast.rate_7d > 0:
                horizon = forecast.hours_to_cap_7d
                if forecast.hours_until_7d_reset is not None:
                    horizon = min(horizon, forecast.hours_until_7d_reset)
                if horizon != float("inf") and horizon > 0:
                    end_time = forecast.latest_timestamp + timedelta(hours=horizon)
                    end_value = forecast.current_7d + forecast.rate_7d * horizon
                    ax1.plot(
                        [forecast.latest_timestamp, end_time],
                        [forecast.current_7d, min(100, end_value)],
                        linestyle="--",
                        color=color,
                        alpha=0.85,
                    )
            if forecast.hits_7d_before_reset and forecast.hours_to_cap_7d != float("inf"):
                limit_time = forecast.latest_timestamp + timedelta(hours=forecast.hours_to_cap_7d)
                ax1.scatter(limit_time, 100, color=color, marker="x", zorder=5)

    ax1.axhline(100, color="#e03131", linestyle="--", linewidth=2, label="Limit")
    ax1.set_title("7-Day Overall Utilization")
    ax1.set_ylabel("Usage %")
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    plt.setp(ax1.get_xticklabels(), rotation=45, ha="right")
    ax1.grid(alpha=0.3)
    ax1.legend(loc="upper left", frameon=False)
    ax1.set_ylim(0, 110)

    # Panel 2: 7-day Opus utilization trend
    ax2 = fig.add_subplot(gs[0, 1])
    for account in accounts:
        acc_data = df[df["account"] == account]
        if acc_data.empty:
            continue
        forecast = forecast_map.get(account)
        (line,) = ax2.plot(
            acc_data["queried_at"],
            acc_data["seven_day_opus_utilization"],
            marker="o",
            linewidth=2,
            markersize=3,
            label=f"{account}",
        )
        color = line.get_color()
        if forecast:
            if forecast.reset_opus_at is not None and not pd.isna(forecast.reset_opus_at):
                ax2.axvline(forecast.reset_opus_at, color=color, linestyle=":", alpha=0.35)
            if forecast.rate_opus > 0:
                horizon = forecast.hours_to_cap_opus
                if forecast.hours_until_opus_reset is not None:
                    horizon = min(horizon, forecast.hours_until_opus_reset)
                if horizon != float("inf") and horizon > 0:
                    end_time = forecast.latest_timestamp + timedelta(hours=horizon)
                    end_value = forecast.current_opus + forecast.rate_opus * horizon
                    ax2.plot(
                        [forecast.latest_timestamp, end_time],
                        [forecast.current_opus, min(100, end_value)],
                        linestyle="--",
                        color=color,
                        alpha=0.85,
                    )
            if forecast.hits_opus_before_reset and forecast.hours_to_cap_opus != float("inf"):
                limit_time = forecast.latest_timestamp + timedelta(hours=forecast.hours_to_cap_opus)
                ax2.scatter(limit_time, 100, color=color, marker="x", zorder=5)

    ax2.axhline(100, color="#e03131", linestyle="--", linewidth=2)
    ax2.set_title("7-Day Opus Utilization")
    ax2.set_ylabel("Usage %")
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    plt.setp(ax2.get_xticklabels(), rotation=45, ha="right")
    ax2.grid(alpha=0.3)
    ax2.legend(loc="upper left", frameon=False)
    ax2.set_ylim(0, 110)

    # Panel 3: Upcoming resets vs limits timeline
    ax3 = fig.add_subplot(gs[1, 0])
    y_positions = {account: idx for idx, account in enumerate(accounts)}
    event_times = []
    for account in accounts:
        forecast = forecast_map.get(account)
        if not forecast:
            continue
        y = y_positions[account]
        if forecast.reset_7d_at is not None and not pd.isna(forecast.reset_7d_at):
            ax3.scatter(forecast.reset_7d_at, y, marker="^", color="#1c7ed6", s=70, zorder=4)
            event_times.append(forecast.reset_7d_at)
        if forecast.reset_opus_at is not None and not pd.isna(forecast.reset_opus_at):
            ax3.scatter(forecast.reset_opus_at, y, marker="v", color="#7048e8", s=70, zorder=4)
            event_times.append(forecast.reset_opus_at)
        if forecast.hits_7d_before_reset and forecast.hours_to_cap_7d != float("inf"):
            limit_time = forecast.latest_timestamp + timedelta(hours=forecast.hours_to_cap_7d)
            ax3.scatter(limit_time, y, marker="x", color="#e03131", s=80, zorder=5)
            event_times.append(limit_time)
        if forecast.hits_opus_before_reset and forecast.hours_to_cap_opus != float("inf"):
            limit_time = forecast.latest_timestamp + timedelta(hours=forecast.hours_to_cap_opus)
            ax3.scatter(limit_time, y, marker="x", color="#f59f00", s=80, zorder=5)
            event_times.append(limit_time)

    if not event_times:
        event_times = [df["queried_at"].max()]
    min_time = min(event_times) - timedelta(hours=6)
    max_time = max(event_times) + timedelta(hours=6)

    ax3.set_yticks(list(y_positions.values()))
    ax3.set_yticklabels(accounts)
    ax3.set_ylim(-0.5, len(accounts) - 0.5)
    ax3.set_xlim(min_time, max_time)
    ax3.set_xlabel("Date / Time")
    ax3.set_title("Upcoming Resets vs Limits")
    ax3.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    plt.setp(ax3.get_xticklabels(), rotation=45, ha="right")
    ax3.grid(axis="x", alpha=0.3)

    legend_handles = [
        Line2D(
            [0],
            [0],
            marker="^",
            linestyle="",
            markerfacecolor="#1c7ed6",
            markeredgecolor="#1c7ed6",
            markersize=9,
            label="7d reset",
        ),
        Line2D(
            [0],
            [0],
            marker="v",
            linestyle="",
            markerfacecolor="#7048e8",
            markeredgecolor="#7048e8",
            markersize=9,
            label="Opus reset",
        ),
        Line2D(
            [0],
            [0],
            marker="x",
            linestyle="",
            color="#e03131",
            markersize=9,
            label="7d limit",
        ),
        Line2D(
            [0],
            [0],
            marker="x",
            linestyle="",
            color="#f59f00",
            markersize=9,
            label="Opus limit",
        ),
    ]
    ax3.legend(handles=legend_handles, loc="upper left", frameon=False, ncol=2)

    # Panel 4: 5-hour utilization trend
    ax4 = fig.add_subplot(gs[1, 1])
    for account in accounts:
        acc_data = df[df["account"] == account]
        if acc_data.empty:
            continue
        ax4.plot(
            acc_data["queried_at"],
            acc_data["five_hour_utilization"],
            linewidth=2,
            label=f"{account}",
        )
    ax4.axhline(100, color="#e03131", linestyle=":", linewidth=2)
    ax4.set_title("5-Hour Window Utilization")
    ax4.set_ylabel("Usage %")
    ax4.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    plt.setp(ax4.get_xticklabels(), rotation=45, ha="right")
    ax4.grid(alpha=0.3)
    ax4.legend(loc="upper left", frameon=False)
    ax4.set_ylim(0, 110)

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


def generate_usage_report(
    db_path: Path,
    output_path: Path,
    window_hours: int = 24,
    show: bool = False,
) -> None:
    if not db_path.exists():
        console.print(f"[bold red]Database not found:[/] {db_path}")
        return

    console.print(f"[bold cyan]Loading usage history from[/] {db_path}")
    df = load_usage_history(db_path)

    if df.empty:
        console.print("[yellow]No usage history found.[/]")
        return

    forecasts: list[AccountForecast] = []
    for account in df["account"].unique():
        acc_df = df[df["account"] == account]
        forecast = forecast_account(acc_df, window_hours)
        if forecast:
            forecasts.append(forecast)

    if not forecasts:
        console.print("[yellow]Not enough data to produce a forecast.[/]")
        return

    severity_order = {
        "🔴 Critical": 0,
        "🟡 Watch": 1,
        "🟢 OK": 2,
        "🟢 Reset": 3,
    }

    def sort_key(f: AccountForecast) -> tuple:
        if f.first_limit_type:
            horizon = f.first_limit_hours
        else:
            reset_candidates = [
                val for val in (f.hours_until_7d_reset, f.hours_until_opus_reset) if val is not None
            ]
            horizon = min(reset_candidates) if reset_candidates else float("inf")
        return (
            severity_order.get(f.status, 2),
            horizon,
        )

    forecasts.sort(key=sort_key)

    console.print(overall_status_panel(forecasts))
    console.print()
    console.print(fleet_capacity_panel(forecasts))
    console.print()
    console.print(accounts_table(forecasts))
    console.print()
    console.print(quick_recos_panel(forecasts))

    console.print("\n[bold cyan]Building visualization…[/]")
    create_visualizations(df, forecasts, output_path, show)
