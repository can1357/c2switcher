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
from rich.columns import Columns
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


@dataclass
class FleetMetrics:
    total_accounts: int
    status_counts: Counter[str]
    total_rate_7d: float
    total_rate_opus: float
    required_overall: int
    required_opus: int
    recommended_fleet: int
    headroom: int
    shortfall: int
    at_risk_accounts: list[str]
    soonest_limit: Optional[tuple[str, str, float]]
    nearest_reset: Optional[float]


def _usage_color(value: Optional[float]) -> str:
    if value is None:
        return "dim"
    if value >= 90:
        return "red"
    if value >= 70:
        return "yellow"
    if value >= 40:
        return "green"
    return "cyan"


def _colorize_percent(value: Optional[float]) -> str:
    if value is None:
        return "[dim]--[/dim]"
    color = _usage_color(value)
    return f"[{color}]{value:.0f}%[/]"


def _colorize_rate(rate: float) -> str:
    if rate >= 2:
        return f"[red]{rate:.2f}%/h[/red]"
    if rate >= 1:
        return f"[yellow]{rate:.2f}%/h[/yellow]"
    if rate > 0:
        return f"[green]{rate:.2f}%/h[/green]"
    return "[dim]0.00%/h[/dim]"


def _usage_bar(value: Optional[float], width: int = 12) -> str:
    if value is None:
        return "[dim]" + "·" * width + "[/dim]"
    capped = max(0.0, min(100.0, value))
    filled = int(round((capped / 100.0) * width))
    filled = min(width, max(0, filled))
    bar = "█" * filled + "·" * (width - filled)
    color = _usage_color(capped)
    return f"[{color}]{bar}[/{color}]"


def compute_fleet_metrics(forecasts: Iterable[AccountForecast]) -> FleetMetrics:
    forecasts = list(forecasts)
    status_counts: Counter[str] = Counter(f.status for f in forecasts)
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

    at_risk_accounts = [f.account for f in forecasts if f.hits_7d_before_reset or f.hits_opus_before_reset]

    limit_candidates = [
        (f.account, f.first_limit_type, f.first_limit_hours)
        for f in forecasts
        if f.first_limit_type and f.first_limit_hours != float("inf")
    ]
    soonest_limit = min(limit_candidates, key=lambda item: item[2]) if limit_candidates else None

    reset_candidates = [
        value
        for f in forecasts
        for value in (f.hours_until_7d_reset, f.hours_until_opus_reset)
        if value is not None
    ]
    nearest_reset = min(reset_candidates) if reset_candidates else None

    headroom = max(0, len(forecasts) - recommended_fleet) if recommended_fleet else len(forecasts)
    shortfall = max(0, recommended_fleet - len(forecasts))

    return FleetMetrics(
        total_accounts=len(forecasts),
        status_counts=status_counts,
        total_rate_7d=total_rate_7d,
        total_rate_opus=total_rate_opus,
        required_overall=required_overall,
        required_opus=required_opus,
        recommended_fleet=recommended_fleet,
        headroom=headroom,
        shortfall=shortfall,
        at_risk_accounts=at_risk_accounts,
        soonest_limit=soonest_limit,
        nearest_reset=nearest_reset,
    )


def fleet_snapshot_panel(metrics: FleetMetrics) -> Panel:
    status_order = ["🔴 Critical", "🟡 Watch", "🟢 OK", "🟢 Reset"]
    status_parts = []
    for status in status_order:
        count = metrics.status_counts.get(status, 0)
        if count:
            status_parts.append(f"{count}× {status}")
    remaining = [
        (status, count) for status, count in metrics.status_counts.items() if status not in status_order and count
    ]
    for status, count in remaining:
        status_parts.append(f"{count}× {status}")

    headline_parts = [f"[bold]{metrics.total_accounts}[/] account(s) online"]
    if status_parts:
        headline_parts.append(" / ".join(status_parts))

    lines = [" ".join(headline_parts)]

    lines.append(
        f"Burn (7d/Opus): {metrics.total_rate_7d:.2f}%/h / {metrics.total_rate_opus:.2f}%/h"
    )

    if metrics.recommended_fleet:
        lines.append(f"Recommended fleet: {metrics.recommended_fleet} account(s)")
        if metrics.shortfall > 0:
            lines.append(f"[red]Shortfall:[/] add {metrics.shortfall} account(s) (Opus/Sonnet burn exceeds supply)")
        else:
            lines.append(f"Headroom: {metrics.headroom} account(s)")
    else:
        lines.append("Recommended fleet: [dim]idle[/dim]")

    if metrics.at_risk_accounts:
        soonest = metrics.soonest_limit
        if soonest:
            account, label, hours = soonest
            lines.append(f"Soonest limit: {account} {label} in {format_horizon(hours)}")
        else:
            lines.append("Some accounts may cap before reset; monitor burn closely.")
    else:
        lines.append("No accounts projected to hit limits before their resets.")

    if metrics.nearest_reset is not None:
        lines.append(f"Nearest reset: {format_horizon(metrics.nearest_reset)}")

    return Panel("\n".join(lines), title="Fleet Snapshot", border_style="cyan", box=box.ROUNDED)


def account_card(forecast: AccountForecast) -> Panel:
    status_border = {
        "🔴 Critical": "red",
        "🟡 Watch": "yellow",
        "🟢 OK": "green",
        "🟢 Reset": "green",
    }.get(forecast.status, "cyan")

    lines = [
        f"{forecast.status} [bold]{forecast.account}[/]",
        f"{_usage_bar(forecast.current_7d)} {_colorize_percent(forecast.current_7d)} "
        f"7d • {_colorize_rate(forecast.rate_7d)}",
        f"{_usage_bar(forecast.current_opus)} {_colorize_percent(forecast.current_opus)} "
        f"Opus • {_colorize_rate(forecast.rate_opus)}",
    ]

    lines.append(
        f"Resets: 7d {format_horizon(forecast.hours_until_7d_reset)} • "
        f"Opus {format_horizon(forecast.hours_until_opus_reset)}"
    )

    if forecast.first_limit_type:
        lines.append(f"Limit: {format_horizon(forecast.first_limit_hours)} → {forecast.first_limit_type}")
    else:
        lines.append("Limit: Resets first")

    gap = forecast.current_opus - forecast.current_7d
    if abs(gap) >= 8:
        if gap > 0:
            lines.append(f"Opus +{abs(gap):.0f}% vs 7d")
        else:
            lines.append(f"7d +{abs(gap):.0f}% vs Opus")

    if forecast.current_5h >= 60:
        lines.append(f"5h window {forecast.current_5h:.0f}% — consider resting soon.")

    lines.append(f"Next: {forecast.headline}")

    return Panel("\n".join(lines), border_style=status_border, box=box.ROUNDED, padding=(0, 1))


def account_overview_columns(forecasts: Iterable[AccountForecast]) -> Columns:
    cards = [account_card(f) for f in forecasts]
    return Columns(cards, expand=True, equal=True)


def limit_outlook_table(forecasts: Iterable[AccountForecast]) -> Optional[Table]:
    rows = []
    severity_order = {
        "🔴 Critical": 0,
        "🟡 Watch": 1,
        "🟢 OK": 2,
        "🟢 Reset": 3,
    }

    for f in forecasts:
        reset_candidates = [
            val for val in (f.hours_until_7d_reset, f.hours_until_opus_reset) if val is not None
        ]
        reset_eta = min(reset_candidates) if reset_candidates else None

        if f.first_limit_type:
            threat = f.first_limit_type
            eta_hours = f.first_limit_hours
            eta_display = format_horizon(f.first_limit_hours)
        else:
            threat = "Resets first"
            eta_hours = reset_eta if reset_eta is not None else float("inf")
            eta_display = f"Reset in {format_horizon(reset_eta)}" if reset_eta is not None else "Steady"

        rows.append(
            {
                "forecast": f,
                "severity": severity_order.get(f.status, 2),
                "eta_hours": eta_hours,
                "eta_display": eta_display,
                "threat": threat,
                "reset_eta": reset_eta,
            }
        )

    rows.sort(key=lambda item: (item["severity"], item["eta_hours"]))

    table = Table(
        title="Limit Outlook",
        box=box.MINIMAL_DOUBLE_HEAD,
        show_lines=False,
        pad_edge=False,
    )
    table.add_column("Account", style="bold")
    table.add_column("Threat", justify="left")
    table.add_column("ETA", justify="left")
    table.add_column("Reset", justify="right")
    table.add_column("7d", justify="right")
    table.add_column("Opus", justify="right")
    table.add_column("7d Rate", justify="right")
    table.add_column("Opus Rate", justify="right")

    for item in rows:
        f = item["forecast"]
        reset_display = format_horizon(item["reset_eta"]) if item["reset_eta"] is not None else "—"
        table.add_row(
            f"{f.status} {f.account}",
            item["threat"],
            item["eta_display"],
            reset_display,
            _colorize_percent(f.current_7d),
            _colorize_percent(f.current_opus),
            _colorize_rate(f.rate_7d),
            _colorize_rate(f.rate_opus),
        )

    return table


def playbook_panel(forecasts: Iterable[AccountForecast], metrics: FleetMetrics) -> Panel:
    lines = []

    if metrics.shortfall > 0:
        lines.append(f"[bold red]Add {metrics.shortfall} account(s) to match current Opus/Sonnet burn.[/bold red]")

    for f in forecasts:
        if f.first_limit_type:
            horizon = format_horizon(f.first_limit_hours)
            if f.first_limit_hours < 6:
                lines.append(f"[bold]{f.account}[/] will cap {f.first_limit_type} in {horizon} — rotate now.")
            elif f.first_limit_hours < 24:
                lines.append(f"[bold]{f.account}[/] hits {f.first_limit_type} in {horizon} — prep fallback.")
            else:
                lines.append(f"[bold]{f.account}[/] trending toward {f.first_limit_type} in {horizon}; monitor.")
        elif f.rate_opus > 0.5 or f.rate_7d > 0.5:
            lines.append(
                f"[bold]{f.account}[/] burning quickly but resets first — check sessions (~{f.rate_opus:.2f}%/h Opus)."
            )
        if f.current_5h > 80:
            lines.append(f"[bold]{f.account}[/] five-hour window {f.current_5h:.0f}% — give it breathing room.")

    if not lines:
        lines = ["All accounts have comfortable margins. Keep monitoring periodically."]

    body = "\n".join(f"- {line}" for line in lines)
    return Panel(body, title="Playbook", border_style="cyan", box=box.ROUNDED)


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

    metrics = compute_fleet_metrics(forecasts)

    console.print(fleet_snapshot_panel(metrics))
    console.print()
    console.print(account_overview_columns(forecasts))
    console.print()
    console.print(limit_outlook_table(forecasts))
    console.print()
    console.print(playbook_panel(forecasts, metrics))

    console.print("\n[bold cyan]Building visualization…[/]")
    create_visualizations(df, forecasts, output_path, show)
