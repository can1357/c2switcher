"""Rich formatting helpers for c2switcher presentation layer."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from rich import box
from rich.panel import Panel
from rich.table import Table

from ..core.models import Account, SelectionDecision, Session


def format_usage_value(value: Optional[int]) -> str:
   """Format usage value with color-coded percentage."""
   if value is None:
      return "[dim]--[/dim]"
   if value >= 90:
      return f"[red]{value}%[/red]"
   if value >= 70:
      return f"[yellow]{value}%[/yellow]"
   return f"[green]{value}%[/green]"


def format_duration(seconds: float) -> str:
   """Format duration in human-readable form: '5m', '2h 30m', '1d 3h'."""
   if seconds < 60:
      return f"{int(seconds)}s"
   if seconds < 3600:
      return f"{int(seconds / 60)}m"
   if seconds < 86400:
      hours = int(seconds / 3600)
      minutes = int((seconds % 3600) / 60)
      if minutes > 0:
         return f"{hours}h {minutes}m"
      return f"{hours}h"
   days = int(seconds / 86400)
   hours = int((seconds % 86400) / 3600)
   if hours > 0:
      return f"{days}d {hours}h"
   return f"{days}d"


def format_time_ago(dt: datetime) -> str:
   """Format datetime as relative time: '5m ago', '2h ago', '3d ago'."""
   now = datetime.now() if dt.tzinfo is None else datetime.now(timezone.utc)
   time_ago = now - dt

   seconds = time_ago.total_seconds()
   if seconds < 60:
      return f"{int(seconds)}s ago"
   if seconds < 3600:
      return f"{int(seconds / 60)}m ago"
   if seconds < 86400:
      return f"{int(seconds / 3600)}h ago"
   return f"{int(seconds / 86400)}d ago"


def render_accounts_table(accounts: List[Account]) -> Table:
   """Render accounts list as Rich table."""
   table = Table(title="Claude Code Accounts", box=box.ROUNDED)
   table.add_column("Index", style="cyan", justify="center")
   table.add_column("Nickname", style="magenta")
   table.add_column("Email", style="green")
   table.add_column("Name", style="blue")
   table.add_column("Type", justify="center")
   table.add_column("Tier", style="yellow")

   for acc in accounts:
      account_type = "Max" if acc.has_claude_max else "Pro" if acc.has_claude_pro else "Free"
      type_color = "green" if acc.has_claude_max else "blue" if acc.has_claude_pro else "dim"

      table.add_row(
         str(acc.index_num),
         acc.nickname or "[dim]--[/dim]",
         acc.email,
         acc.display_name or acc.full_name or "[dim]--[/dim]",
         f"[{type_color}]{account_type}[/{type_color}]",
         acc.rate_limit_tier or "[dim]--[/dim]",
      )

   return table


def render_sessions_table(sessions: List[Session]) -> Table:
   """Render active sessions as Rich table."""
   from ..data.store import Store

   table = Table(title="Active Claude Sessions", box=box.ROUNDED)
   table.add_column("Session ID", style="cyan")
   table.add_column("Account", style="green")
   table.add_column("PID", style="yellow")
   table.add_column("Working Directory", style="blue")
   table.add_column("Started", style="magenta")

   store = Store()
   try:
      for session in sessions:
         account_email = "not assigned"
         if session.account_uuid:
            acc = store.get_account(session.account_uuid)
            if acc:
               account_email = acc.email

         started_dt = _parse_timestamp(session.created_at)
         started_str = format_time_ago(started_dt) if started_dt else "unknown"

         session_id_short = session.session_id[:8] + "..."

         cwd = session.cwd or "unknown"
         if len(cwd) > 40:
            cwd = "..." + cwd[-37:]

         table.add_row(
            session_id_short,
            account_email,
            str(session.pid),
            cwd,
            started_str,
         )
   finally:
      store.close()

   return table


def render_usage_table(usage_data: List[Dict[str, Any]]) -> Table:
   """Render usage data across accounts as Rich table."""
   from ..utils import format_time_until_reset

   table = Table(title="Usage Across Accounts", box=box.ROUNDED)
   table.add_column("Index", style="cyan", justify="center")
   table.add_column("Nickname", style="magenta")
   table.add_column("Email", style="green")
   table.add_column("5h", justify="right")
   table.add_column("7d", justify="right")
   table.add_column("7d Opus", justify="right")
   table.add_column("Reset (Rate)", justify="right", no_wrap=True)
   table.add_column("Sessions", style="blue", justify="center")

   for item in usage_data:
      acc = item["account"]
      usage_info = item.get("usage")
      sessions = item.get("sessions", 0)

      session_str = f"[blue]{sessions}[/blue]" if sessions > 0 else "[dim]0[/dim]"

      if usage_info is None:
         table.add_row(
            str(acc.index_num if hasattr(acc, 'index_num') else acc.get("index_num")),
            (acc.nickname if hasattr(acc, 'nickname') else acc.get("nickname")) or "[dim]--[/dim]",
            acc.email if hasattr(acc, 'email') else acc.get("email"),
            "[red]Error[/red]",
            "[red]Error[/red]",
            "[red]Error[/red]",
            "[red]Error[/red]",
            session_str,
         )
         continue

      five_hour = usage_info.get("five_hour", {}) or {}
      seven_day = usage_info.get("seven_day", {}) or {}
      seven_day_opus = usage_info.get("seven_day_opus", {}) or {}

      opus_util = seven_day_opus.get("utilization")
      overall_util = seven_day.get("utilization")
      reset_time = format_time_until_reset(
         seven_day_opus.get("resets_at") if seven_day_opus else None,
         seven_day.get("resets_at"),
         opus_util if opus_util is not None else 0,
         overall_util if overall_util is not None else 0,
      )

      table.add_row(
         str(acc.index_num if hasattr(acc, 'index_num') else acc.get("index_num")),
         (acc.nickname if hasattr(acc, 'nickname') else acc.get("nickname")) or "[dim]--[/dim]",
         acc.email if hasattr(acc, 'email') else acc.get("email"),
         format_usage_value(five_hour.get("utilization")),
         format_usage_value(seven_day.get("utilization")),
         format_usage_value(seven_day_opus.get("utilization")),
         reset_time,
         session_str,
      )

   return table


def render_session_history_table(sessions: List[Dict[str, Any]]) -> Table:
   """Render session history with usage deltas as Rich table."""
   from ..data.store import Store

   table = Table(title="Session History", box=box.ROUNDED)
   table.add_column("Account", style="cyan")
   table.add_column("Project Path", style="blue")
   table.add_column("Duration", style="magenta", justify="right")
   table.add_column("Opus Δ", style="yellow", justify="right")
   table.add_column("Overall Δ", style="yellow", justify="right")
   table.add_column("Ended", style="dim", justify="right")

   store = Store()
   try:
      for session in sessions:
         account_display = "[dim]unknown[/dim]"
         account_uuid = session.get("account_uuid")

         if account_uuid:
            acc = store.get_account(account_uuid)
            if acc:
               nickname = acc.nickname or ""
               index = acc.index_num
               account_display = f"[{index}] {nickname or acc.email}"

         cwd = session.get("cwd") or "unknown"
         if len(cwd) > 45:
            cwd = "..." + cwd[-42:]

         duration_seconds = session.get("duration_seconds", 0)
         duration_str = format_duration(duration_seconds)

         opus_delta = session.get("opus_delta", "[dim]--[/dim]")
         overall_delta = session.get("overall_delta", "[dim]--[/dim]")

         if isinstance(opus_delta, (int, float)):
            opus_delta = (
               f"[red]+{opus_delta}%[/red]" if opus_delta > 0
               else f"[green]{opus_delta}%[/green]" if opus_delta < 0
               else "[dim]0%[/dim]"
            )

         if isinstance(overall_delta, (int, float)):
            overall_delta = (
               f"[red]+{overall_delta}%[/red]" if overall_delta > 0
               else f"[green]{overall_delta}%[/green]" if overall_delta < 0
               else "[dim]0%[/dim]"
            )

         ended = session.get("ended_at")
         ended_dt = _parse_timestamp(ended)
         ended_str = format_time_ago(ended_dt) if ended_dt else "unknown"

         table.add_row(
            account_display,
            cwd,
            duration_str,
            opus_delta,
            overall_delta,
            ended_str,
         )
   finally:
      store.close()

   return table


def render_account_panel(account: Account, **kwargs) -> Panel:
   """Render account details as Rich panel."""
   nickname = account.nickname or "[dim]none[/dim]"
   masked_email = account.mask_email()

   content = (
      f"[green]Account (={account.index_num})[/green]\n\n"
      f"Nickname: [bold]{nickname}[/bold]\n"
      f"Email: [bold]{masked_email}[/bold]\n"
      f"Name: {account.display_name or account.full_name or '[dim]--[/dim]'}"
   )

   title = kwargs.get("title", "Account Details")
   border_style = kwargs.get("border_style", "green")

   return Panel(content, title=title, border_style=border_style)


def render_selection_panel(decision: SelectionDecision, verbose: bool = False) -> Panel:
   """Render selection decision as Rich panel with metrics."""
   account = decision.account
   nickname = account.nickname or "[dim]none[/dim]"
   masked_email = account.mask_email()

   tier_label = f"Tier {decision.tier}" if decision.tier else "N/A"

   info_text = (
      f"[green]Optimal Account (={account.index_num}) - {tier_label}[/green]\n\n"
      f"Nickname: [bold]{nickname}[/bold]\n"
      f"Email: [bold]{masked_email}[/bold]\n"
      f"Opus Usage:    {int(decision.opus_usage or 0):>3}%\n"
      f"Overall Usage: {int(decision.overall_usage or 0):>3}%"
   )

   if decision.drain_rate is not None and decision.headroom is not None and decision.hours_to_reset is not None:
      info_text += f"\nDrain: {decision.drain_rate:.2f}%/h | Headroom: {decision.headroom:.0f}% | Reset: {decision.hours_to_reset:.0f}h"

   if verbose:
      if decision.priority_drain is not None and decision.fresh_bonus is not None:
         info_text += (
            f"\n[dim]Priority Drain: {decision.priority_drain:.3f} %/h "
            f"(fresh bonus {decision.fresh_bonus:.2f})[/dim]"
         )
      if decision.adjusted_drain is not None:
         five_hour_penalty = decision.five_hour_factor - 1.0 if decision.five_hour_factor else 0.0
         info_text += (
            f"\n[dim]Adjusted Drain: {decision.adjusted_drain:.3f} %/h "
            f"(5h pen {five_hour_penalty:.2f})[/dim]"
         )
      if decision.expected_burst is not None:
         info_text += f"\n[dim]Burst Buffer: {decision.expected_burst:.1f}%[/dim]"
      if decision.five_hour_utilization is not None:
         info_text += f"\n[dim]5h Utilization: {decision.five_hour_utilization:.1f}%[/dim]"
      if decision.cache_source:
         cache_info = decision.cache_source
         if decision.cache_age_seconds is not None:
            cache_info += f" ({decision.cache_age_seconds:.0f}s old)"
         info_text += f"\n[dim]Usage source: {cache_info}[/dim]"

   session_info = ""
   if decision.reused:
      session_info = "\n[cyan]↻ Session reused existing assignment[/cyan]"
   elif decision.active_sessions or decision.recent_sessions:
      active = decision.active_sessions or 0
      recent = decision.recent_sessions or 0
      session_info = f"\n[dim]Sessions: {active} active, {recent} recent (5min)[/dim]"

   info_text += session_info

   return Panel(info_text, border_style="green")


def _parse_timestamp(timestamp: Optional[str]) -> Optional[datetime]:
   """Parse ISO timestamp to datetime, handling None and errors gracefully."""
   if not timestamp:
      return None
   try:
      dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
      if dt.tzinfo is None:
         dt = dt.replace(tzinfo=timezone.utc)
      return dt.astimezone().replace(tzinfo=None)
   except Exception:
      return None
