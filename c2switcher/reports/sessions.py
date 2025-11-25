"""Modern session analytics report with polished visuals."""

from __future__ import annotations

import sqlite3
import webbrowser
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from rich import box
from rich.columns import Columns
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()
_WORKTREE_REPO_CACHE: Dict[Tuple[Path, str], str] = {}


def _resolve_worktree_repo(base_dir: Path, worktree_name: str) -> str:
    segments = worktree_name.split('-')
    for length in range(len(segments), 0, -1):
        candidate = '-'.join(segments[:length])
        if (base_dir / candidate).is_dir():
            return candidate
    return segments[0] if segments else worktree_name


def extract_project(path: Optional[str]) -> str:
    if not path:
        return 'unknown'

    p = Path(path)
    parts = p.parts

    # Collapse git worktree folders back to repo names
    try:
        wt_idx = parts.index('.worktrees')
    except ValueError:
        pass
    else:
        worktree_name = parts[wt_idx + 1] if len(parts) > wt_idx + 1 else ''
        base_dir = Path(*parts[:wt_idx]) if wt_idx > 0 else Path('/')
        if worktree_name:
            key = (base_dir, worktree_name)
            repo_name = _WORKTREE_REPO_CACHE.get(key)
            if repo_name is None:
                repo_name = _resolve_worktree_repo(base_dir, worktree_name)
                _WORKTREE_REPO_CACHE[key] = repo_name
            return repo_name or worktree_name

    if 'Projects' in parts:
        idx = parts.index('Projects')
        if idx + 1 < len(parts):
            return parts[idx + 1]

    last = p.name
    return last if last else 'unknown'


def load_sessions(db_path: Path, min_duration_sec: int, days: int) -> pd.DataFrame:
    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
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
    created_at = pd.to_datetime(df['created_at'], utc=True).dt.tz_convert(local_tz).dt.tz_localize(None)
    ended_at = pd.to_datetime(df['ended_at'], utc=True).dt.tz_convert(local_tz).dt.tz_localize(None)

    df = df.assign(
        created_at=created_at,
        ended_at=ended_at,
        duration_min=(ended_at - created_at).dt.total_seconds() / 60,
    )

    df = df[df['duration_min'] >= min_duration_sec / 60]

    if days > 0:
        cutoff = datetime.now() - timedelta(days=days)
        df = df[df['created_at'] >= cutoff]

    df = df.assign(
        project=df['cwd'].apply(extract_project),
        account=df['nickname'].fillna(df['display_name']).fillna('unknown'),
        date=df['created_at'].dt.date,
        hour=df['created_at'].dt.hour,
        weekday=df['created_at'].dt.day_name(),
    )

    return df


def format_relative_time(moment: Optional[datetime]) -> str:
    if moment is None:
        return '—'
    delta = datetime.now() - moment
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 60:
        return f'{seconds}s ago'
    minutes = seconds // 60
    if minutes < 60:
        return f'{minutes}m ago'
    hours = minutes // 60
    if hours < 24:
        return f'{hours}h ago'
    days = hours // 24
    if days < 14:
        return f'{days}d ago'
    weeks = days // 7
    if weeks < 8:
        return f'{weeks}w ago'
    months = days // 30
    if months < 18:
        return f'{months}mo ago'
    years = days // 365
    return f'{years}y ago'


@dataclass
class SessionMetrics:
    total_sessions: int
    total_hours: float
    total_minutes: float
    unique_projects: int
    unique_accounts: int
    span_days: int
    avg_daily_hours: float
    median_session_min: float
    longest_project: str
    longest_account: str
    longest_minutes: float
    first_session: datetime
    last_session: datetime
    recent_hours: float
    recent_sessions: int
    recent_projects: List[tuple[str, int]]
    busiest_hour: Optional[int]
    busiest_day: Optional[str]


def compute_session_metrics(df: pd.DataFrame) -> SessionMetrics:
    total_minutes = float(df['duration_min'].sum())
    total_hours = total_minutes / 60
    total_sessions = len(df)
    unique_projects = df['project'].nunique()
    unique_accounts = df['account'].nunique()
    first_session = df['created_at'].min()
    last_session = df['created_at'].max()
    span_days = max(1, (last_session.date() - first_session.date()).days + 1)
    avg_daily_hours = total_hours / span_days if span_days else 0.0
    median_session = float(df['duration_min'].median()) if total_sessions else 0.0
    longest_idx = df['duration_min'].idxmax()
    longest_row = df.loc[longest_idx]
    longest_project = longest_row['project']
    longest_account = longest_row['account']
    longest_minutes = float(longest_row['duration_min'])

    recent_window = last_session - timedelta(days=7)
    recent_df = df[df['created_at'] >= recent_window]
    recent_hours = recent_df['duration_min'].sum() / 60 if not recent_df.empty else 0.0
    recent_sessions = int(recent_df['session_id'].nunique()) if not recent_df.empty else 0
    recent_projects = Counter(recent_df['project']).most_common(3) if not recent_df.empty else []

    busiest_hour = int(df.groupby('hour')['duration_min'].sum().idxmax()) if total_sessions else None
    day_totals = df.groupby('weekday')['duration_min'].sum().sort_values(ascending=False)
    busiest_day = day_totals.index[0] if not day_totals.empty else None

    return SessionMetrics(
        total_sessions=total_sessions,
        total_hours=total_hours,
        total_minutes=total_minutes,
        unique_projects=unique_projects,
        unique_accounts=unique_accounts,
        span_days=span_days,
        avg_daily_hours=avg_daily_hours,
        median_session_min=median_session,
        longest_project=longest_project,
        longest_account=longest_account,
        longest_minutes=longest_minutes,
        first_session=first_session,
        last_session=last_session,
        recent_hours=recent_hours,
        recent_sessions=recent_sessions,
        recent_projects=recent_projects,
        busiest_hour=busiest_hour,
        busiest_day=busiest_day,
    )


def activity_snapshot_panel(metrics: SessionMetrics) -> Panel:
    lines = [
        f'[bold]{metrics.total_sessions}[/] sessions across {metrics.span_days} day(s)',
        f'Focus time: [bold]{metrics.total_hours:.1f}h[/] ({metrics.avg_daily_hours:.1f}h/day)',
        f'Projects: {metrics.unique_projects} • Accounts: {metrics.unique_accounts}',
        f'Median session: {metrics.median_session_min:.0f}m • Longest: {metrics.longest_minutes:.0f}m '
        f'({metrics.longest_project}, {metrics.longest_account})',
        f'First session: {metrics.first_session:%Y-%m-%d} ({format_relative_time(metrics.first_session)})',
        f'Latest session: {metrics.last_session:%Y-%m-%d %H:%M} ({format_relative_time(metrics.last_session)})',
    ]
    if metrics.busiest_hour is not None:
        lines.append(f'Peak hour: {metrics.busiest_hour:02d}:00')
    if metrics.busiest_day:
        lines.append(f'Loudest day: {metrics.busiest_day}')
    return Panel(
        '\n'.join(lines),
        title='Activity Snapshot',
        border_style='cyan',
        box=box.ROUNDED,
    )


def project_cards(df: pd.DataFrame, limit: int = 6) -> Columns | Panel:
    project_stats = (
        df.groupby('project')
        .agg(
            minutes=('duration_min', 'sum'),
            sessions=('session_id', 'count'),
            median_min=('duration_min', 'median'),
            avg_min=('duration_min', 'mean'),
            last_seen=('created_at', 'max'),
            accounts=('account', 'nunique'),
        )
        .sort_values('minutes', ascending=False)
    )

    if project_stats.empty:
        return Panel(
            'No project activity recorded.',
            title='Project Highlights',
            border_style='magenta',
            box=box.ROUNDED,
        )

    total_minutes = project_stats['minutes'].sum()
    cards = []
    for project, row in project_stats.head(limit).iterrows():
        share = row['minutes'] / total_minutes if total_minutes else 0
        if share >= 0.35:
            border = 'magenta'
        elif share >= 0.18:
            border = 'green'
        else:
            border = 'cyan'
        lines = [
            f'[bold]{project}[/]',
            f'{row["minutes"] / 60:.1f}h total ({share * 100:.0f}%)',
            f'{int(row["sessions"])} session(s) • {int(row["accounts"])} account(s)',
            f'Median {row["median_min"]:.0f}m • Avg {row["avg_min"]:.0f}m',
            f'Last active {format_relative_time(row["last_seen"])}',
        ]
        cards.append(Panel('\n'.join(lines), border_style=border, box=box.ROUNDED, padding=(0, 1)))

    columns = Columns(cards, expand=True, equal=True)
    return Panel(columns, title='Project Highlights', border_style='magenta', box=box.ROUNDED)


def project_detail_table(df: pd.DataFrame) -> Table:
    totals = df['duration_min'].sum()
    project_stats = (
        df.groupby('project')
        .agg(
            minutes=('duration_min', 'sum'),
            sessions=('session_id', 'count'),
            median_min=('duration_min', 'median'),
            avg_min=('duration_min', 'mean'),
            accounts=('account', 'nunique'),
        )
        .sort_values('minutes', ascending=False)
        .head(10)
    )

    table = Table(
        title='Top Focus Areas',
        box=box.MINIMAL_DOUBLE_HEAD,
        pad_edge=False,
        show_lines=False,
    )
    table.add_column('Project', justify='left', style='bold')
    table.add_column('Hours', justify='right')
    table.add_column('Share', justify='right')
    table.add_column('Sessions', justify='right')
    table.add_column('Median', justify='right')
    table.add_column('Avg', justify='right')
    table.add_column('Accounts', justify='right')

    for project, row in project_stats.iterrows():
        share = (row['minutes'] / totals * 100) if totals else 0
        table.add_row(
            project,
            f'{row["minutes"] / 60:.1f}',
            f'{share:4.1f}%',
            f'{int(row["sessions"])}',
            f'{row["median_min"]:.0f}m',
            f'{row["avg_min"]:.0f}m',
            f'{int(row["accounts"])}',
        )
    return table


def focus_windows_table(df: pd.DataFrame) -> Table:
    totals = df['duration_min'].sum()
    hour_stats = df.groupby('hour')['duration_min'].sum().reindex(range(24), fill_value=0).sort_values(ascending=False)
    top_hours = hour_stats.head(6)

    table = Table(
        title='Peak Focus Windows',
        box=box.SIMPLE_HEAVY,
        pad_edge=False,
    )
    table.add_column('Hour', justify='center')
    table.add_column('Hours', justify='right')
    table.add_column('Share', justify='right')
    table.add_column('Sessions', justify='right')
    table.add_column('Signature Project', justify='left')

    for hour, minutes in top_hours.items():
        share = (minutes / totals * 100) if totals else 0
        sessions = int((df['hour'] == hour).sum())
        project = df[df['hour'] == hour].groupby('project')['duration_min'].sum().sort_values(ascending=False).head(1)
        project_name = project.index[0] if not project.empty else '—'
        table.add_row(
            f'{hour:02d}:00',
            f'{minutes / 60:.1f}h',
            f'{share:4.1f}%',
            str(sessions),
            project_name,
        )
    return table


def account_mix_table(df: pd.DataFrame) -> Table:
    totals = df['duration_min'].sum()
    account_stats = (
        df.groupby('account')
        .agg(
            minutes=('duration_min', 'sum'),
            sessions=('session_id', 'count'),
            unique_projects=('project', 'nunique'),
        )
        .sort_values('minutes', ascending=False)
    )

    table = Table(
        title='Account Distribution',
        box=box.SQUARE,
        pad_edge=False,
    )
    table.add_column('Account', style='bold')
    table.add_column('Hours', justify='right')
    table.add_column('Share', justify='right')
    table.add_column('Sessions', justify='right')
    table.add_column('Projects', justify='right')
    table.add_column('Focus Anchor', justify='left')

    for account, row in account_stats.iterrows():
        minutes = row['minutes']
        share = (minutes / totals * 100) if totals else 0
        account_df = df[df['account'] == account]
        focus_project = account_df.groupby('project')['duration_min'].sum().sort_values(ascending=False).head(1)
        focus_name = focus_project.index[0] if not focus_project.empty else '—'
        table.add_row(
            account,
            f'{minutes / 60:.1f}',
            f'{share:4.1f}%',
            f'{int(row["sessions"])}',
            f'{int(row["unique_projects"])}',
            focus_name,
        )
    return table


def momentum_panel(df: pd.DataFrame, metrics: SessionMetrics) -> Panel:
    if metrics.recent_sessions == 0:
        body = 'No sessions recorded in the last 7 days.'
    else:
        per_day = metrics.recent_hours / 7
        project_summary = ', '.join(f'[bold]{name}[/] ({count}×)' for name, count in metrics.recent_projects)
        body = (
            f'Last 7 days: [bold]{metrics.recent_hours:.1f}[/] hrs '
            f'({metrics.recent_sessions} sessions, {per_day:.1f}h/day)\n'
            f'Top projects: {project_summary if project_summary else "—"}'
        )
    return Panel(body, title='Recent Momentum', border_style='magenta', box=box.ROUNDED)


def recommendations_panel(df: pd.DataFrame, metrics: SessionMetrics) -> Panel:
    lines: List[str] = []
    latest_sessions = df.sort_values('created_at', ascending=False).head(5)
    if not latest_sessions.empty:
        recent_projects = Counter(latest_sessions['project'])
        fav, count = recent_projects.most_common(1)[0]
        lines.append(
            f'Keep momentum on [bold]{fav}[/] — {count} of your last {len(latest_sessions)} sessions touched it.'
        )

    if metrics.busiest_hour is not None:
        lines.append(f'Protect {metrics.busiest_hour:02d}:00 — it consistently delivers the densest focus blocks.')
    if metrics.busiest_day:
        lines.append(f'Schedule deep work on {metrics.busiest_day}s; they capture the most total minutes.')

    if metrics.median_session_min < 45:
        lines.append('Median session is under 45 minutes — consider batching similar work to limit context switches.')
    if metrics.unique_projects > 8:
        lines.append('High project spread detected — archive or cluster low-priority projects to regain focus.')
    if metrics.longest_minutes > 180:
        lines.append('Frequent >3h sessions. Block recovery time afterwards to avoid burnout.')

    project_totals = df.groupby('project')['duration_min'].sum().sort_values(ascending=False)
    total_minutes = project_totals.sum()
    if total_minutes > 0:
        project_share = project_totals / total_minutes
    else:
        project_share = pd.Series(dtype=float)
    if not project_share.empty and project_share.iloc[0] >= 0.6:
        lines.append(
            f'[bold]{project_share.index[0]}[/] accounts for {project_share.iloc[0] * 100:.0f}% of focus time — '
            'consider spreading risk to a secondary project.'
        )

    if not lines:
        lines.append('No critical adjustments surfaced. Keep the current rhythm.')

    body = '\n'.join(f'- {line}' for line in lines)
    return Panel(body, title='Playbook', border_style='yellow', box=box.ROUNDED)


def create_visualizations(df: pd.DataFrame, output_path: Path, days: int, show: bool) -> None:
    plt.style.use('seaborn-v0_8')

    fig = plt.figure(figsize=(18, 12))
    fig.suptitle('C2Switcher Session Insights', fontsize=18, fontweight='bold')
    gs = fig.add_gridspec(2, 2, height_ratios=[1, 1.1], wspace=0.25, hspace=0.3)

    # Panel 1: Daily hours trend
    ax1 = fig.add_subplot(gs[0, 0])
    daily = df.groupby('date')['duration_min'].sum() / 60
    if days > 0:
        daily = daily.tail(days)
    rolling = daily.rolling(window=7, min_periods=1).mean()
    ax1.plot(
        daily.index,
        daily.values,
        marker='o',
        linewidth=2,
        label='Daily hours',
        color='#2b8a3e',
    )
    ax1.plot(
        rolling.index,
        rolling.values,
        linestyle='--',
        linewidth=2,
        label='7-day avg',
        color='#1971c2',
    )
    ax1.fill_between(daily.index, daily.values, color='#2b8a3e', alpha=0.2)
    ax1.set_xlabel('Date')
    ax1.set_ylabel('Hours')
    ax1.set_title('Daily Focus Time')
    ax1.grid(alpha=0.3)
    ax1.legend(frameon=False)
    ax1.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d'))
    plt.setp(ax1.get_xticklabels(), rotation=45, ha='right')

    # Panel 2: Top projects bar
    ax2 = fig.add_subplot(gs[0, 1])
    project_hours = df.groupby('project')['duration_min'].sum().sort_values(ascending=False) / 60
    top_projects = project_hours.head(8)[::-1]
    bars = ax2.barh(top_projects.index, top_projects.values, color='#f08c00')
    ax2.set_xlabel('Total hours')
    ax2.set_title('Top Projects')
    for bar, value in zip(bars, top_projects.values):
        ax2.text(
            value + 0.1,
            bar.get_y() + bar.get_height() / 2,
            f'{value:.1f}h',
            va='center',
        )

    # Panel 3: Heatmap hour vs weekday
    ax3 = fig.add_subplot(gs[1, 0])
    weekdays = [
        'Monday',
        'Tuesday',
        'Wednesday',
        'Thursday',
        'Friday',
        'Saturday',
        'Sunday',
    ]
    pivot = (
        df.assign(weekday=pd.Categorical(df['weekday'], categories=weekdays, ordered=True))
        .pivot_table(
            index='weekday',
            columns='hour',
            values='duration_min',
            aggfunc='sum',
            fill_value=0,
            observed=False,
        )
        .reindex(weekdays)
        .fillna(0)
    )
    data = pivot.values
    im = ax3.imshow(data, aspect='auto', cmap='YlGnBu')
    ax3.set_title('Energy by Weekday & Hour')
    ax3.set_xlabel('Hour of day')
    ax3.set_ylabel('Weekday')
    ax3.set_xticks(range(0, 24, 2))
    ax3.set_xticklabels([f'{h:02d}' for h in range(0, 24, 2)])
    ax3.set_yticks(range(len(pivot.index)))
    ax3.set_yticklabels(pivot.index)
    cbar = plt.colorbar(im, ax=ax3, shrink=0.8)
    cbar.set_label('Minutes')

    # Panel 4: Session length distribution
    ax4 = fig.add_subplot(gs[1, 1])
    durations = df['duration_min']
    bins = np.linspace(0, min(240, durations.max() + 10), 30)
    ax4.hist(durations, bins=bins, color='#748ffc', edgecolor='white', alpha=0.9)
    ax4.axvline(durations.median(), color='#e03131', linestyle='--', linewidth=2, label='Median')
    ax4.axvline(durations.mean(), color='#2f9e44', linestyle=':', linewidth=2, label='Mean')
    ax4.set_xlabel('Session length (minutes)')
    ax4.set_ylabel('Count')
    ax4.set_title('Session Duration Distribution')
    ax4.legend(frameon=False)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=200, bbox_inches='tight')
    console.print(f'[bold green]✓[/] Saved visualization to [link=file://{output_path}]{output_path}[/]')

    if output_path.exists():
        console.print(f'[dim]Figure size: {output_path.stat().st_size / 1024:.1f} KiB[/]')

    webbrowser.open(f'file://{output_path.absolute()}')

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
        console.print(f'[bold red]Database not found:[/] {db_path}')
        return

    console.print(f'[bold cyan]Loading sessions from[/] {db_path}')
    df = load_sessions(db_path, min_duration, days)

    if df.empty:
        console.print('[yellow]No sessions found that match the filters.[/]')
        return

    metrics = compute_session_metrics(df)

    console.print(activity_snapshot_panel(metrics))
    console.print()
    console.print(project_cards(df))
    console.print()
    console.print(project_detail_table(df))
    console.print()
    console.print(focus_windows_table(df))
    console.print()
    console.print(account_mix_table(df))
    console.print()
    console.print(momentum_panel(df, metrics))
    console.print()
    console.print(recommendations_panel(df, metrics))

    console.print('\n[bold cyan]Generating visualization…[/]')
    create_visualizations(df, output_path, days, show)
