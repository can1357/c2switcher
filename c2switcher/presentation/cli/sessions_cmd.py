"""Session management commands."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

import click
from rich import box
from rich.table import Table

from ...constants import console
from ...infrastructure.locking import acquire_lock
from ...infrastructure.factory import ServiceFactory


def _parse_sqlite_timestamp_to_local(timestamp_str: str) -> datetime:
   """Parse SQLite UTC timestamp to naive local datetime."""
   dt = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
   return dt.astimezone().replace(tzinfo=None)


@click.command(name="start-session")
@click.option("--session-id", required=True, help="Unique session identifier")
@click.option("--pid", required=True, type=int, help="Process ID")
@click.option("--parent-pid", type=int, help="Parent process ID")
@click.option("--cwd", required=True, help="Current working directory")
def start_session_cmd(session_id: str, pid: int, parent_pid: Optional[int], cwd: str):
   """Register a new Claude session."""
   acquire_lock()
   factory = ServiceFactory()
   try:
      session_service = factory.get_session_service()
      session_service.register(session_id, pid, parent_pid, cwd)
   finally:
      factory.close()


@click.command(name="end-session")
@click.option("--session-id", required=True, help="Session identifier to end")
def end_session(session_id: str):
   """Mark a Claude session as ended."""
   acquire_lock()
   factory = ServiceFactory()

   try:
      store = factory.get_store()
      store.mark_session_ended(session_id)
   except Exception as exc:
      console.print(f"[yellow]Warning: Failed to end session: {exc}[/yellow]")
   finally:
      factory.close()


@click.command(name="sessions")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
def list_sessions(output_json: bool):
   """List active Claude sessions."""
   factory = ServiceFactory()

   try:
      session_service = factory.get_session_service()
      session_service.maybe_cleanup()

      active_sessions = session_service.list_active()

      if not active_sessions:
         if output_json:
            print(json.dumps({"sessions": []}))
         else:
            console.print("[yellow]No active sessions[/yellow]")
         return

      if output_json:
         json_sessions = []
         for session in active_sessions:
            account_email = None
            account_index = None
            if session.account_uuid:
               store = factory.get_store()
               acc = store.get_account_by_identifier(session.account_uuid)
               if acc:
                  account_email = acc.email
                  account_index = acc.index_num

            started_dt = _parse_sqlite_timestamp_to_local(session.created_at)
            json_sessions.append({
               "session_id": session.session_id,
               "account_email": account_email,
               "account_index": account_index,
               "pid": session.pid,
               "cwd": session.cwd,
               "started_at": session.created_at,
               "age_seconds": (datetime.now() - started_dt).total_seconds(),
            })
         print(json.dumps({"sessions": json_sessions, "total": len(json_sessions)}, indent=2))
         return

      table = Table(title="Active Claude Sessions", box=box.ROUNDED)
      table.add_column("Session ID", style="cyan")
      table.add_column("Account", style="green")
      table.add_column("PID", style="yellow")
      table.add_column("Working Directory", style="blue")
      table.add_column("Started", style="magenta")

      for session in active_sessions:
         account_email = "not assigned"
         if session.account_uuid:
            store = factory.get_store()
            acc = store.get_account_by_identifier(session.account_uuid)
            if acc:
               account_email = acc.email

         started_dt = _parse_sqlite_timestamp_to_local(session.created_at)

         time_ago = datetime.now() - started_dt
         if time_ago.total_seconds() < 60:
            started_str = f"{int(time_ago.total_seconds())}s ago"
         elif time_ago.total_seconds() < 3600:
            started_str = f"{int(time_ago.total_seconds() / 60)}m ago"
         else:
            started_str = f"{int(time_ago.total_seconds() / 3600)}h ago"

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

      console.print(table)
      console.print(f"\n[dim]Total active sessions: {len(active_sessions)}[/dim]")

   except Exception as exc:
      console.print(f"[red]Error: {exc}[/red]")
   finally:
      factory.close()


@click.command(name="session-history")
@click.option("--limit", default=20, type=int, help="Maximum number of sessions to show")
@click.option("--min-duration", default=5, type=int, help="Minimum session duration in seconds")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
def session_history(limit: int, min_duration: int, output_json: bool):
   """Show historical sessions with usage deltas."""
   factory = ServiceFactory()

   try:
      store = factory.get_store()
      sessions_raw = store.get_session_history(min_duration_seconds=min_duration, limit=limit)

      # Convert Session models to dict for easier processing
      sessions = []
      for session in sessions_raw:
         # Calculate duration from timestamps
         from datetime import datetime
         created = datetime.fromisoformat(session.created_at.replace("Z", "+00:00"))
         ended = datetime.fromisoformat(session.ended_at.replace("Z", "+00:00"))
         duration_seconds = (ended - created).total_seconds()

         sessions.append({
            "session_id": session.session_id,
            "account_uuid": session.account_uuid,
            "cwd": session.cwd,
            "duration_seconds": duration_seconds,
            "created_at": session.created_at,
            "ended_at": session.ended_at,
         })

      if not sessions:
         if output_json:
            print(json.dumps({"sessions": []}))
         else:
            console.print(f"[yellow]No sessions found with duration >= {min_duration}s[/yellow]")
         return

      if output_json:
         json_sessions = []
         for session in sessions:
            account_index = None
            account_nickname = None
            account_email = None
            sonnet_delta_value = None
            overall_delta_value = None

            account_uuid = session["account_uuid"]
            if account_uuid:
               acc = store.get_account_by_uuid(account_uuid)
               if acc:
                  account_index = acc.index_num
                  account_nickname = acc.nickname
                  account_email = acc.email

               usage_before = store.get_usage_before(account_uuid, session["created_at"])
               usage_after = store.get_usage_after(account_uuid, session["ended_at"])

               if usage_before and usage_after:
                  before_sonnet = usage_before["data"].get("seven_day_sonnet", {}) or {}
                  after_sonnet = usage_after["data"].get("seven_day_sonnet", {}) or {}
                  before_sonnet_pct = before_sonnet.get("utilization")
                  after_sonnet_pct = after_sonnet.get("utilization")
                  if before_sonnet_pct is not None and after_sonnet_pct is not None:
                     sonnet_delta_value = after_sonnet_pct - before_sonnet_pct

                  before_overall = usage_before["data"].get("seven_day", {}) or {}
                  after_overall = usage_after["data"].get("seven_day", {}) or {}
                  before_overall_pct = before_overall.get("utilization")
                  after_overall_pct = after_overall.get("utilization")
                  if before_overall_pct is not None and after_overall_pct is not None:
                     overall_delta_value = after_overall_pct - before_overall_pct

            json_sessions.append({
               "session_id": session["session_id"],
               "account_index": account_index,
               "account_nickname": account_nickname,
               "account_email": account_email,
               "cwd": session["cwd"],
               "duration_seconds": session["duration_seconds"],
               "sonnet_delta": sonnet_delta_value,
               "overall_delta": overall_delta_value,
               "created_at": session["created_at"],
               "ended_at": session["ended_at"],
            })

         print(json.dumps({"sessions": json_sessions, "total": len(json_sessions)}, indent=2))
         return

      table = Table(title=f"Session History (duration >= {min_duration}s)", box=box.ROUNDED)
      table.add_column("Account", style="cyan")
      table.add_column("Project Path", style="blue")
      table.add_column("Duration", style="magenta", justify="right")
      table.add_column("Sonnet Δ", style="yellow", justify="right")
      table.add_column("Overall Δ", style="yellow", justify="right")
      table.add_column("Ended", style="dim", justify="right")

      for session in sessions:
         account_display = "[dim]unknown[/dim]"
         account_uuid = session["account_uuid"]

         if account_uuid:
            acc = store.get_account_by_uuid(account_uuid)
            if acc:
               nickname = acc.nickname or ""
               index = acc.index_num
               account_display = f"[{index}] {nickname or acc.email}"

         cwd = session["cwd"] or "unknown"
         if len(cwd) > 45:
            cwd = "..." + cwd[-42:]

         duration_seconds = session["duration_seconds"]
         if duration_seconds < 60:
            duration_str = f"{int(duration_seconds)}s"
         elif duration_seconds < 3600:
            duration_str = f"{int(duration_seconds / 60)}m"
         else:
            hours = int(duration_seconds / 3600)
            minutes = int((duration_seconds % 3600) / 60)
            duration_str = f"{hours}h {minutes}m"

         sonnet_delta = "[dim]--[/dim]"
         overall_delta = "[dim]--[/dim]"

         if account_uuid:
            usage_before = store.get_usage_before(account_uuid, session["created_at"])
            usage_after = store.get_usage_after(account_uuid, session["ended_at"])

            if usage_before and usage_after:
               before_sonnet = usage_before["data"].get("seven_day_sonnet", {}) or {}
               after_sonnet = usage_after["data"].get("seven_day_sonnet", {}) or {}
               before_sonnet_pct = before_sonnet.get("utilization")
               after_sonnet_pct = after_sonnet.get("utilization")
               if before_sonnet_pct is not None and after_sonnet_pct is not None:
                  delta = after_sonnet_pct - before_sonnet_pct
                  sonnet_delta = (
                     f"[red]+{delta}%[/red]" if delta > 0 else f"[green]{delta}%[/green]" if delta < 0 else "[dim]0%[/dim]"
                  )

               before_overall = usage_before["data"].get("seven_day", {}) or {}
               after_overall = usage_after["data"].get("seven_day", {}) or {}
               before_overall_pct = before_overall.get("utilization")
               after_overall_pct = after_overall.get("utilization")
               if before_overall_pct is not None and after_overall_pct is not None:
                  delta = after_overall_pct - before_overall_pct
                  overall_delta = (
                     f"[red]+{delta}%[/red]"
                     if delta > 0
                     else f"[green]{delta}%[/green]" if delta < 0 else "[dim]0%[/dim]"
                  )

         ended_dt = _parse_sqlite_timestamp_to_local(session["ended_at"])

         time_ago = datetime.now() - ended_dt
         if time_ago.total_seconds() < 60:
            ended_str = f"{int(time_ago.total_seconds())}s ago"
         elif time_ago.total_seconds() < 3600:
            ended_str = f"{int(time_ago.total_seconds() / 60)}m ago"
         elif time_ago.total_seconds() < 86400:
            ended_str = f"{int(time_ago.total_seconds() / 3600)}h ago"
         else:
            ended_str = f"{int(time_ago.total_seconds() / 86400)}d ago"

         table.add_row(
            account_display,
            cwd,
            duration_str,
            sonnet_delta,
            overall_delta,
            ended_str,
         )

      console.print(table)
      console.print(f"\n[dim]Total sessions: {len(sessions)}[/dim]")

   except Exception as exc:
      console.print(f"[red]Error: {exc}[/red]")
   finally:
      factory.close()
