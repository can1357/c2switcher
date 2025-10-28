"""Command-line interface for the c2switcher tool."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
import time
from typing import Optional

import click
import requests
from rich import box
from rich.panel import Panel
from rich.table import Table

from .api import ClaudeAPI
from .constants import C2SWITCHER_DIR, CLAUDE_DIR, CREDENTIALS_PATH, console
from .database import Database
from .load_balancer import select_account_with_load_balancing
from .locking import acquire_lock
from .reports import generate_session_report, generate_usage_report
from .sessions import cleanup_dead_sessions, register_session
from .tokens import refresh_token
from .usage import get_account_usage
from .utils import (
    atomic_write_json,
    format_time_until_reset,
    mask_email,
    parse_sqlite_timestamp_to_local,
)

DEFAULT_DB_PATH = Path.home() / ".c2switcher" / "store.db"
DEFAULT_SESSION_OUTPUT = Path.home() / "c2switcher_session_report.png"
DEFAULT_USAGE_OUTPUT = Path.home() / "c2switcher_usage_report.png"


@click.group()
def cli():
    """Claude Code Account Switcher - Manage multiple Claude Code accounts."""


@cli.command()
@click.option("--nickname", "-n", help="Optional nickname for the account")
@click.option("--creds-file", "-f", type=click.Path(exists=True), help="Path to credentials JSON file")
def add(nickname: Optional[str], creds_file: Optional[str]):
    """Add a new account from credentials file or current .credentials.json."""
    acquire_lock()
    db = Database()

    try:
        if creds_file:
            with open(creds_file, "r", encoding="utf-8") as handle:
                credentials = json.load(handle)
        else:
            if not CREDENTIALS_PATH.exists():
                console.print(f"[red]Error: {CREDENTIALS_PATH} not found[/red]")
                console.print("[yellow]Please specify a credentials file with --creds-file[/yellow]")
                return
            with open(CREDENTIALS_PATH, "r", encoding="utf-8") as handle:
                credentials = json.load(handle)

        expires_at = credentials.get("claudeAiOauth", {}).get("expiresAt", 0)
        if expires_at <= int(time.time() * 1000):
            credentials = refresh_token(json.dumps(credentials), account_uuid=None)

        token = credentials.get("claudeAiOauth", {}).get("accessToken")
        if not token:
            console.print("[red]Error: No access token found in credentials[/red]")
            return

        try:
            with console.status("[bold green]Fetching account profile..."):
                profile = ClaudeAPI.get_profile(token)
        except requests.exceptions.HTTPError as exc:
            if exc.response.status_code == 401:
                console.print("[yellow]Token rejected, attempting refresh...[/yellow]")
                credentials = refresh_token(json.dumps(credentials), account_uuid=None)
                token = credentials.get("claudeAiOauth", {}).get("accessToken")
                with console.status("[bold green]Retrying profile fetch..."):
                    profile = ClaudeAPI.get_profile(token)
            else:
                raise

        account_uuid = profile.get("account", {}).get("uuid")
        if not account_uuid:
            console.print("[red]Error: No account UUID in profile[/red]")
            return

        credentials = refresh_token(json.dumps(credentials), account_uuid)
        index = db.add_account(profile, credentials, nickname)

        account = profile.get("account", {})
        console.print(
            Panel(
                f"[green]✓[/green] Account added successfully\n\n"
                f"Index: [bold]{index}[/bold]\n"
                f"Email: [bold]{account.get('email')}[/bold]\n"
                f"Name: {account.get('full_name')}\n"
                f"Nickname: {nickname or '[dim]none[/dim]'}",
                title="Account Added",
                border_style="green",
            )
        )

    except Exception as exc:
        console.print(f"[red]Error adding account: {exc}[/red]")
    finally:
        db.close()


@cli.command()
@click.option("--switch", is_flag=True, help="Actually switch to the optimal account")
@click.option("--session-id", help="Session ID for load balancing and sticky assignment")
@click.option("--token-only", is_flag=True, help="Output only the token to stdout")
def optimal(switch: bool, session_id: Optional[str], token_only: bool):
    """Find the optimal account with load balancing and session stickiness."""
    if switch or session_id:
        acquire_lock()

    db = Database()

    try:
        result = select_account_with_load_balancing(db, session_id)

        if not result:
            console.print("[red]No accounts available (all at capacity or no accounts found)[/red]")
            return

        account = result["account"]
        optimal_creds = None
        token = None

        if switch or token_only:
            credentials = json.loads(account["credentials_json"])
            account_info = {
                "uuid": account["uuid"],
                "email": account["email"],
                "org_uuid": account["org_uuid"],
                "display_name": account["display_name"],
                "billing_type": account["billing_type"],
                "org_name": account["org_name"],
            }
            refreshed_creds = refresh_token(json.dumps(credentials), account["uuid"], account_info)
            token = refreshed_creds.get("claudeAiOauth", {}).get("accessToken")

            if not token:
                console.print("[red]Error: No access token found in credentials[/red]")
                return

            if refreshed_creds != credentials:
                cursor = db.conn.cursor()
                cursor.execute(
                    "UPDATE accounts SET credentials_json = ?, updated_at = CURRENT_TIMESTAMP WHERE uuid = ?",
                    (json.dumps(refreshed_creds), account["uuid"]),
                )
                db.conn.commit()
            optimal_creds = refreshed_creds

        nickname = account["nickname"] or "[dim]none[/dim]"
        masked_email = mask_email(account["email"])

        tier_label = f"Tier {result['tier']}" if "tier" in result else "N/A"
        session_info = ""

        if result.get("reused"):
            session_info = "\n[cyan]↻ Session reused existing assignment[/cyan]"
        elif "active_sessions" in result or "recent_sessions" in result:
            active = result.get("active_sessions", 0)
            recent = result.get("recent_sessions", 0)
            session_info = f"\n[dim]Sessions: {active} active, {recent} recent (5min)[/dim]"

        info_text = (
            f"[green]Optimal Account (={account['index_num']}) - {tier_label}[/green]\n\n"
            f"Nickname: [bold]{nickname}[/bold]\n"
            f"Email: [bold]{masked_email}[/bold]\n"
            f"Opus Usage:    {result.get('opus_usage', 0):>3}%\n"
            f"Overall Usage: {result.get('overall_usage', 0):>3}%"
        )

        if "drain_rate" in result:
            info_text += f"\n[dim]Drain Rate: {result['drain_rate']:.3f} %/h[/dim]"
        if "adjusted_drain" in result and "five_hour_penalty" in result:
            info_text += (
                f"\n[dim]Adjusted Drain: {result['adjusted_drain']:.3f} %/h (pen {result['five_hour_penalty']:.2f})[/dim]"
            )
        if "headroom" in result:
            info_text += f"\n[dim]Headroom: {result['headroom']:.1f}%[/dim]"
        if "expected_burst" in result:
            info_text += f"\n[dim]Burst Buffer: {result['expected_burst']:.1f}%[/dim]"
        if "five_hour_utilization" in result:
            info_text += f"\n[dim]5h Utilization: {result['five_hour_utilization']:.1f}%[/dim]"
        if "hours_to_reset" in result:
            info_text += f"\n[dim]Hours to Reset: {result['hours_to_reset']:.1f}[/dim]"
        if result.get("cache_source"):
            cache_info = result["cache_source"]
            if result.get("cache_age_seconds") is not None:
                cache_info += f" ({result['cache_age_seconds']:.0f}s old)"
            info_text += f"\n[dim]Usage source: {cache_info}[/dim]"

        info_text += session_info

        if token_only:
            console.print(Panel(info_text, border_style="green"))
            print(token)
        elif switch:
            CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
            atomic_write_json(CREDENTIALS_PATH, optimal_creds)
            console.print(Panel(info_text, border_style="green"))
            if not session_id:
                console.print("[green]✓[/green] Switched to optimal account")
        else:
            console.print(Panel(info_text, border_style="green"))

    except Exception as exc:
        console.print(f"[red]Error: {exc}[/red]")
    finally:
        db.close()


@cli.command()
@click.argument("identifier", required=False)
@click.option("--session-id", help="Session ID for load balancing (when no identifier given)")
@click.option("--token-only", is_flag=True, help="Output only the token to stdout")
def switch(identifier: Optional[str], session_id: Optional[str], token_only: bool):
    """Switch to a specific account by index, nickname, email, or UUID."""
    if not identifier and not session_id:
        console.print("[red]Error: Must provide either an identifier or --session-id for load balancing[/red]")
        return

    if not token_only or session_id:
        acquire_lock()

    db = Database()

    try:
        if identifier:
            account = db.get_account_by_identifier(identifier)
            if not account:
                console.print(f"[red]Account not found: {identifier}[/red]")
                return
        else:
            result = select_account_with_load_balancing(db, session_id)
            if not result:
                console.print("[red]No accounts available (all at capacity or no accounts found)[/red]")
                return
            account = result["account"]

        credentials = json.loads(account["credentials_json"])

        account_info = {
            "uuid": account["uuid"],
            "email": account["email"],
            "org_uuid": account["org_uuid"],
            "display_name": account["display_name"],
            "billing_type": account["billing_type"],
            "org_name": account["org_name"],
        }
        refreshed_creds = refresh_token(json.dumps(credentials), account["uuid"], account_info)
        token = refreshed_creds.get("claudeAiOauth", {}).get("accessToken")

        if not token:
            console.print("[red]Error: No access token found in credentials[/red]")
            return

        if refreshed_creds != credentials:
            cursor = db.conn.cursor()
            cursor.execute(
                "UPDATE accounts SET credentials_json = ?, updated_at = CURRENT_TIMESTAMP WHERE uuid = ?",
                (json.dumps(refreshed_creds), account["uuid"]),
            )
            db.conn.commit()

        nickname = account["nickname"] or "[dim]none[/dim]"
        masked_email = mask_email(account["email"])

        panel_content = (
            f"[green]Switched to account (={account['index_num']})[/green]\n\n"
            f"Nickname: [bold]{nickname}[/bold]\n"
            f"Email: [bold]{masked_email}[/bold]"
        )

        if token_only:
            console.print(Panel(panel_content, border_style="green"))
            print(token)
        else:
            CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
            atomic_write_json(CREDENTIALS_PATH, refreshed_creds)
            console.print(Panel(panel_content, border_style="green"))

    finally:
        db.close()


@cli.command(name="force-refresh")
@click.argument("identifier", required=False)
def force_refresh(identifier: Optional[str]):
    """Force refresh tokens for an account (or all accounts if none specified)."""
    db = Database()

    try:
        if identifier:
            account = db.get_account_by_identifier(identifier)
            if not account:
                console.print(f"[red]Account not found: {identifier}[/red]")
                return
            accounts_to_refresh = [account]
        else:
            cursor = db.conn.cursor()
            cursor.execute(
                """
                SELECT uuid, email, nickname, credentials_json, index_num, org_uuid, display_name, billing_type, org_name
                FROM accounts
                ORDER BY index_num
                """
            )
            rows = cursor.fetchall()
            accounts_to_refresh = [
                {
                    "uuid": row[0],
                    "email": row[1],
                    "nickname": row[2],
                    "credentials_json": row[3],
                    "index_num": row[4],
                    "org_uuid": row[5],
                    "display_name": row[6],
                    "billing_type": row[7],
                    "org_name": row[8],
                }
                for row in rows
            ]

        if not accounts_to_refresh:
            console.print("[yellow]No accounts to refresh[/yellow]")
            return

        console.print(f"[yellow]Force refreshing {len(accounts_to_refresh)} account(s)...[/yellow]\n")

        for account in accounts_to_refresh:
            account_display = f"[{account['index_num']}] {account['nickname'] or account['email']}"

            try:
                account_info = {
                    "uuid": account["uuid"],
                    "email": account["email"],
                    "org_uuid": account["org_uuid"],
                    "display_name": account["display_name"],
                    "billing_type": account["billing_type"],
                    "org_name": account["org_name"],
                }

                refreshed_creds = refresh_token(
                    account["credentials_json"],
                    account["uuid"],
                    account_info,
                    force=True,
                )

                cursor = db.conn.cursor()
                cursor.execute(
                    "UPDATE accounts SET credentials_json = ?, updated_at = CURRENT_TIMESTAMP WHERE uuid = ?",
                    (json.dumps(refreshed_creds), account["uuid"]),
                )
                db.conn.commit()

                expires_at = refreshed_creds.get("claudeAiOauth", {}).get("expiresAt", 0)
                expires_in_hours = (expires_at - int(time.time() * 1000)) / 1000 / 3600

                console.print(f"[green]✓[/green] {account_display} - expires in {expires_in_hours:.1f}h")

            except Exception as exc:
                console.print(f"[red]✗[/red] {account_display} - Error: {exc}")

    except Exception as exc:
        console.print(f"[red]Error: {exc}[/red]")
    finally:
        db.close()


@cli.command()
def cycle():
    """Cycle to the next account in the list."""
    acquire_lock()
    db = Database()

    try:
        accounts = db.get_all_accounts()

        if not accounts:
            console.print("[yellow]No accounts found. Add one with 'c2switcher add'[/yellow]")
            return

        if len(accounts) == 1:
            console.print("[yellow]Only one account available[/yellow]")
            return

        current_uuid = None
        if CREDENTIALS_PATH.exists():
            with open(CREDENTIALS_PATH, "r", encoding="utf-8") as handle:
                try:
                    current_creds = json.load(handle)
                    cursor = db.conn.cursor()
                    for acc in accounts:
                        acc_creds = json.loads(acc["credentials_json"])
                        if acc_creds.get("claudeAiOauth", {}).get("accessToken") == current_creds.get(
                            "claudeAiOauth", {}
                        ).get("accessToken"):
                            current_uuid = acc["uuid"]
                            break
                except Exception:
                    pass

        if current_uuid:
            current_index = None
            for idx, acc in enumerate(accounts):
                if acc["uuid"] == current_uuid:
                    current_index = idx
                    break
            next_account = accounts[(current_index + 1) % len(accounts)] if current_index is not None else accounts[0]
        else:
            next_account = accounts[0]

        account_info = {
            "uuid": next_account["uuid"],
            "email": next_account["email"],
            "org_uuid": next_account["org_uuid"],
            "display_name": next_account["display_name"],
            "billing_type": next_account["billing_type"],
            "org_name": next_account["org_name"],
        }
        refreshed_creds = refresh_token(next_account["credentials_json"], next_account["uuid"], account_info)

        CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write_json(CREDENTIALS_PATH, refreshed_creds)

        console.print(
            Panel(
                f"[green]Switched to next account:[/green] {next_account['nickname'] or next_account['email']}",
                border_style="green",
            )
        )

    finally:
        db.close()


@cli.command(name="start-session")
@click.option("--session-id", required=True, help="Unique session identifier")
@click.option("--pid", required=True, type=int, help="Process ID")
@click.option("--parent-pid", type=int, help="Parent process ID")
@click.option("--cwd", required=True, help="Current working directory")
def start_session_cmd(session_id: str, pid: int, parent_pid: Optional[int], cwd: str):
    """Register a new Claude session."""
    acquire_lock()
    db = Database()
    try:
        register_session(db, session_id, pid, parent_pid, cwd)
    finally:
        db.close()


@cli.command(name="end-session")
@click.option("--session-id", required=True, help="Session identifier to end")
def end_session(session_id: str):
    """Mark a Claude session as ended."""
    acquire_lock()
    db = Database()

    try:
        db.mark_session_ended(session_id)
    except Exception as exc:
        console.print(f"[yellow]Warning: Failed to end session: {exc}[/yellow]")
    finally:
        db.close()


@cli.command(name="sessions")
def list_sessions():
    """List active Claude sessions."""
    db = Database()

    try:
        cleanup_dead_sessions(db)

        active_sessions = db.get_active_sessions()

        if not active_sessions:
            console.print("[yellow]No active sessions[/yellow]")
            return

        table = Table(title="Active Claude Sessions", box=box.ROUNDED)
        table.add_column("Session ID", style="cyan")
        table.add_column("Account", style="green")
        table.add_column("PID", style="yellow")
        table.add_column("Working Directory", style="blue")
        table.add_column("Started", style="magenta")

        for session in active_sessions:
            account_email = "not assigned"
            if session["account_uuid"]:
                acc = db.get_account_by_identifier(session["account_uuid"])
                if acc:
                    account_email = acc["email"]

            started = session["created_at"]
            started_dt = parse_sqlite_timestamp_to_local(started)

            time_ago = datetime.now() - started_dt
            if time_ago.total_seconds() < 60:
                started_str = f"{int(time_ago.total_seconds())}s ago"
            elif time_ago.total_seconds() < 3600:
                started_str = f"{int(time_ago.total_seconds() / 60)}m ago"
            else:
                started_str = f"{int(time_ago.total_seconds() / 3600)}h ago"

            session_id_short = session["session_id"][:8] + "..."

            cwd = session["cwd"] or "unknown"
            if len(cwd) > 40:
                cwd = "..." + cwd[-37:]

            table.add_row(
                session_id_short,
                account_email,
                str(session["pid"]),
                cwd,
                started_str,
            )

        console.print(table)
        console.print(f"\n[dim]Total active sessions: {len(active_sessions)}[/dim]")

    except Exception as exc:
        console.print(f"[red]Error: {exc}[/red]")
    finally:
        db.close()


@cli.command(name="session-history")
@click.option("--limit", default=20, type=int, help="Maximum number of sessions to show")
@click.option("--min-duration", default=5, type=int, help="Minimum session duration in seconds")
def session_history(limit: int, min_duration: int):
    """Show historical sessions with usage deltas."""
    db = Database()

    try:
        sessions = db.get_session_history(min_duration_seconds=min_duration, limit=limit)

        if not sessions:
            console.print(f"[yellow]No sessions found with duration >= {min_duration}s[/yellow]")
            return

        table = Table(title=f"Session History (duration >= {min_duration}s)", box=box.ROUNDED)
        table.add_column("Account", style="cyan")
        table.add_column("Project Path", style="blue")
        table.add_column("Duration", style="magenta", justify="right")
        table.add_column("Opus Δ", style="yellow", justify="right")
        table.add_column("Overall Δ", style="yellow", justify="right")
        table.add_column("Ended", style="dim", justify="right")

        for session in sessions:
            account_display = "[dim]unknown[/dim]"
            account_uuid = session["account_uuid"]

            if account_uuid:
                acc = db.get_account_by_identifier(account_uuid)
                if acc:
                    nickname = acc["nickname"] or ""
                    index = acc["index_num"]
                    account_display = f"[{index}] {nickname or acc['email']}"

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

            opus_delta = "[dim]--[/dim]"
            overall_delta = "[dim]--[/dim]"

            if account_uuid:
                usage_before = db.get_usage_before(account_uuid, session["created_at"])
                usage_after = db.get_usage_after(account_uuid, session["ended_at"])

                if usage_before and usage_after:
                    before_opus = usage_before["data"].get("seven_day_opus", {}) or {}
                    after_opus = usage_after["data"].get("seven_day_opus", {}) or {}
                    before_opus_pct = before_opus.get("utilization")
                    after_opus_pct = after_opus.get("utilization")
                    if before_opus_pct is not None and after_opus_pct is not None:
                        delta = after_opus_pct - before_opus_pct
                        opus_delta = (
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

            ended = session["ended_at"]
            ended_dt = parse_sqlite_timestamp_to_local(ended)

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
                opus_delta,
                overall_delta,
                ended_str,
            )

        console.print(table)
        console.print(f"\n[dim]Total sessions: {len(sessions)}[/dim]")

    except Exception as exc:
        console.print(f"[red]Error: {exc}[/red]")
    finally:
        db.close()


@cli.command(name="report-sessions")
@click.option(
    "--db",
    "db_path",
    type=click.Path(path_type=Path),
    default=DEFAULT_DB_PATH,
    show_default=True,
    help="Path to the c2switcher SQLite database",
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(path_type=Path),
    default=DEFAULT_SESSION_OUTPUT,
    show_default=True,
    help="Where to write the generated PNG report",
)
@click.option(
    "--days",
    type=int,
    default=30,
    show_default=True,
    help="Only include sessions from the last N days (0 = all history)",
)
@click.option(
    "--min-duration",
    type=int,
    default=60,
    show_default=True,
    help="Ignore sessions shorter than this many seconds",
)
@click.option("--show", is_flag=True, help="Display the visualization after rendering (requires GUI backend)")
def report_sessions(db_path: Path, output_path: Path, days: int, min_duration: int, show: bool):
    """Generate the modern session analytics report."""
    generate_session_report(db_path, output_path, days=days, min_duration=min_duration, show=show)


@cli.command(name="report-usage")
@click.option(
    "--db",
    "db_path",
    type=click.Path(path_type=Path),
    default=DEFAULT_DB_PATH,
    show_default=True,
    help="Path to the c2switcher SQLite database",
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(path_type=Path),
    default=DEFAULT_USAGE_OUTPUT,
    show_default=True,
    help="Where to write the generated PNG report",
)
@click.option(
    "--window-hours",
    type=int,
    default=24,
    show_default=True,
    help="History window (in hours) for burn-rate estimation",
)
@click.option("--show", is_flag=True, help="Display the visualization after rendering (requires GUI backend)")
def report_usage(db_path: Path, output_path: Path, window_hours: int, show: bool):
    """Generate the modern usage risk forecast report."""
    generate_usage_report(db_path, output_path, window_hours=window_hours, show=show)


@cli.command(name="history", hidden=True)
@click.pass_context
def history_alias(ctx):
    """Alias for 'session-history'."""
    ctx.forward(session_history)


@cli.command(name="list", hidden=True)
@click.pass_context
def list_alias(ctx):
    """Alias for 'ls'."""
    ctx.forward(list_accounts_cmd)


@cli.command(name="list-accounts", hidden=True)
@click.pass_context
def list_accounts_alias(ctx):
    """Alias for 'ls'."""
    ctx.forward(list_accounts_cmd)


@cli.command(name="pick", hidden=True)
@click.pass_context
def pick(ctx):
    """Alias for 'optimal'."""
    ctx.forward(optimal)


@cli.command(name="use", hidden=True)
@click.pass_context
def use(ctx):
    """Alias for 'switch'."""
    ctx.forward(switch)


@cli.command(name="ls")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
def list_accounts_cmd(output_json: bool):
    """List all accounts."""
    db = Database()

    try:
        accounts = db.get_all_accounts()

        if output_json:
            result = []
            for acc in accounts:
                result.append(
                    {
                        "index": acc["index_num"],
                        "nickname": acc["nickname"],
                        "email": acc["email"],
                        "full_name": acc["full_name"],
                        "display_name": acc["display_name"],
                        "has_claude_max": bool(acc["has_claude_max"]),
                        "has_claude_pro": bool(acc["has_claude_pro"]),
                        "org_type": acc["org_type"],
                        "rate_limit_tier": acc["rate_limit_tier"],
                    }
                )
            print(json.dumps(result, indent=2))
            return

        if not accounts:
            console.print("[yellow]No accounts found. Add one with 'c2switcher add'[/yellow]")
            return

        table = Table(title="Claude Code Accounts", box=box.ROUNDED)
        table.add_column("Index", style="cyan", justify="center")
        table.add_column("Nickname", style="magenta")
        table.add_column("Email", style="green")
        table.add_column("Name", style="blue")
        table.add_column("Type", justify="center")
        table.add_column("Tier", style="yellow")

        for acc in accounts:
            account_type = "Max" if acc["has_claude_max"] else "Pro" if acc["has_claude_pro"] else "Free"
            type_color = "green" if acc["has_claude_max"] else "blue" if acc["has_claude_pro"] else "dim"

            table.add_row(
                str(acc["index_num"]),
                acc["nickname"] or "[dim]--[/dim]",
                acc["email"],
                acc["display_name"] or acc["full_name"] or "[dim]--[/dim]",
                f"[{type_color}]{account_type}[/{type_color}]",
                acc["rate_limit_tier"] or "[dim]--[/dim]",
            )

        console.print(table)

    finally:
        db.close()


@cli.command()
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.option("--force", is_flag=True, help="Force refresh (ignore cache)")
def usage(output_json: bool, force: bool):
    """List usage across all accounts with session distribution."""
    acquire_lock()
    db = Database()

    try:
        cleanup_dead_sessions(db)

        accounts = db.get_all_accounts()

        if not accounts:
            console.print("[yellow]No accounts found. Add one with 'c2switcher add'[/yellow]")
            return

        session_counts = {acc["uuid"]: db.count_active_sessions(acc["uuid"]) for acc in accounts}

        usage_data = []
        for acc in accounts:
            try:
                display_name = acc["nickname"] or acc["email"]
                with console.status(f"[bold green]Fetching usage for {display_name}..."):
                    usage_info = get_account_usage(db, acc["uuid"], acc["credentials_json"], force=force)

                usage_data.append({"account": acc, "usage": usage_info, "sessions": session_counts[acc["uuid"]]})
            except Exception as exc:
                display_name = acc["nickname"] or acc["email"]
                console.print(f"[red]Error fetching usage for {display_name}: {exc}[/red]")
                usage_data.append(
                    {
                        "account": acc,
                        "usage": None,
                        "sessions": session_counts[acc["uuid"]],
                        "error": str(exc),
                    }
                )

        if output_json:
            result = []
            for item in usage_data:
                acc = item["account"]
                result.append(
                    {
                        "index": acc["index_num"],
                        "nickname": acc["nickname"],
                        "email": acc["email"],
                        "usage": item["usage"],
                        "sessions": item["sessions"],
                        "error": item.get("error"),
                    }
                )
            print(json.dumps(result, indent=2))
            return

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
            usage_info = item["usage"]
            sessions = item["sessions"]

            session_str = f"[blue]{sessions}[/blue]" if sessions > 0 else "[dim]0[/dim]"

            if usage_info is None:
                table.add_row(
                    str(acc["index_num"]),
                    acc["nickname"] or "[dim]--[/dim]",
                    acc["email"],
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

            def format_usage(value):
                if value is None:
                    return "[dim]--[/dim]"
                if value >= 90:
                    return f"[red]{value}%[/red]"
                if value >= 70:
                    return f"[yellow]{value}%[/yellow]"
                return f"[green]{value}%[/green]"

            opus_util = seven_day_opus.get("utilization")
            overall_util = seven_day.get("utilization")
            reset_time = format_time_until_reset(
                seven_day_opus.get("resets_at") if seven_day_opus else None,
                seven_day.get("resets_at"),
                opus_util if opus_util is not None else 0,
                overall_util if overall_util is not None else 0,
            )

            table.add_row(
                str(acc["index_num"]),
                acc["nickname"] or "[dim]--[/dim]",
                acc["email"],
                format_usage(five_hour.get("utilization")),
                format_usage(seven_day.get("utilization")),
                format_usage(seven_day_opus.get("utilization")),
                reset_time,
                session_str,
            )

        console.print(table)

        active_sessions = db.get_active_sessions()
        if active_sessions:
            console.print(f"\n[bold]Active Sessions ({len(active_sessions)}):[/bold]")
            for session in active_sessions[:5]:
                account_email = "[dim]not assigned[/dim]"
                if session["account_uuid"]:
                    acc = db.get_account_by_identifier(session["account_uuid"])
                    if acc:
                        account_email = acc["email"]

                started = session["created_at"]
                started_dt = parse_sqlite_timestamp_to_local(started)

                time_ago = datetime.now() - started_dt
                if time_ago.total_seconds() < 60:
                    time_str = f"{int(time_ago.total_seconds())}s ago"
                elif time_ago.total_seconds() < 3600:
                    time_str = f"{int(time_ago.total_seconds() / 60)}m ago"
                else:
                    time_str = f"{int(time_ago.total_seconds() / 3600)}h ago"

                cwd = session["cwd"] or "unknown"
                if len(cwd) > 35:
                    cwd = "..." + cwd[-32:]

                console.print(f"  * {account_email} [dim]({cwd}, {time_str})[/dim]")

            if len(active_sessions) > 5:
                console.print(f"  [dim]... and {len(active_sessions) - 5} more[/dim]")

    finally:
        db.close()
