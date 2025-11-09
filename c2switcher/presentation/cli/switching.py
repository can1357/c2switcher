"""Account switching commands."""

from __future__ import annotations

import json
from typing import Optional

import click
from rich.panel import Panel

from ...constants import console
from ...infrastructure.locking import acquire_lock
from ...infrastructure.factory import ServiceFactory
from ...core.errors import NoAccountsAvailable
from ...utils import mask_email


@click.command()
@click.option("--dry-run", is_flag=True, help="Show optimal account without switching")
@click.option("--session-id", help="Session ID for load balancing and sticky assignment")
@click.option("--token-only", is_flag=True, help="Output only the token to stdout")
@click.option("--with-label", is_flag=True, help="Output label before token (requires --token-only)")
@click.option("--quiet", is_flag=True, help="Suppress panel output (use with --token-only)")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed metrics (drain rates, headroom, etc.)")
def optimal(dry_run: bool, session_id: Optional[str], token_only: bool, with_label: bool, quiet: bool, output_json: bool, verbose: bool):
   """Find and switch to the optimal account with load balancing and session stickiness."""
   acquire_lock()
   factory = ServiceFactory()

   try:
      switching_service = factory.get_switching_service()

      # Decide if we should actually switch
      should_switch = not dry_run

      # Get optimal account
      decision = switching_service.select_optimal(
         session_id=session_id,
         token_only=token_only,
         dry_run=dry_run
      )

      # Extract token if needed
      token = None
      if should_switch or token_only:
         creds = decision.account.get_credentials()
         token = creds.get("claudeAiOauth", {}).get("accessToken")

         if not token:
            if output_json:
               print(json.dumps({"error": "No access token found"}))
            else:
               console.print("[red]Error: No access token found in credentials[/red]")
            return

      # Output results
      if output_json:
         json_output = {
            "index": decision.account.index_num,
            "nickname": decision.account.nickname,
            "email": decision.account.email,
            "tier": decision.tier,
            "opus_usage": decision.opus_usage,
            "overall_usage": decision.overall_usage,
            "switched": should_switch and not token_only,
            "reused_session": decision.reused,
         }
         if decision.active_sessions is not None:
            json_output["active_sessions"] = decision.active_sessions
         if decision.recent_sessions is not None:
            json_output["recent_sessions"] = decision.recent_sessions
         if token_only and token:
            json_output["token"] = token
         print(json.dumps(json_output, indent=2))
         return

      nickname = decision.account.nickname or "[dim]none[/dim]"
      masked_email = mask_email(decision.account.email)

      tier_label = f"Tier {decision.tier}" if decision.tier else "N/A"
      opus_usage = decision.opus_usage if decision.opus_usage is not None else 0
      overall_usage = decision.overall_usage if decision.overall_usage is not None else 0
      session_info = ""

      if decision.reused:
         session_info = "\n[cyan]↻ Session reused existing assignment[/cyan]"
      elif decision.active_sessions is not None or decision.recent_sessions is not None:
         active = decision.active_sessions or 0
         recent = decision.recent_sessions or 0
         session_info = f"\n[dim]Sessions: {active} active, {recent} recent (5min)[/dim]"

      info_text = (
         f"[green]Optimal Account (={decision.account.index_num}) - {tier_label}[/green]\n\n"
         f"Nickname: [bold]{nickname}[/bold]\n"
         f"Email: [bold]{masked_email}[/bold]\n"
         f"Opus Usage:    {opus_usage:>3}%\n"
         f"Overall Usage: {overall_usage:>3}%"
      )

      # Always show: drain rate, headroom, hours to reset
      if decision.drain_rate is not None and decision.headroom is not None and decision.hours_to_reset is not None:
         info_text += f"\nDrain: {decision.drain_rate:.2f}%/h | Headroom: {decision.headroom:.0f}% | Reset: {decision.hours_to_reset:.0f}h"

      if verbose:
         if decision.priority_score is not None and decision.usage_bonus is not None:
            info_text += (
               f"\n[dim]Priority Score: {decision.priority_score:.3f} %/h "
               f"(usage bonus {decision.usage_bonus:.2f}, opus penalty {decision.high_util_penalty:.2f})[/dim]"
            )
         if decision.adjusted_drain is not None and decision.five_hour_factor is not None:
            info_text += (
               f"\n[dim]Adjusted Drain: {decision.adjusted_drain:.3f} %/h "
               f"(5h factor {decision.five_hour_factor:.2f})[/dim]"
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

      info_text += session_info

      # Output
      if token_only:
         if not quiet:
            console.print(Panel(info_text, border_style="green"))
         if with_label:
            print(decision.account.display_identifier())
         print(token)
      else:
         console.print(Panel(info_text, border_style="green"))
         if should_switch and not session_id:
            console.print("[green]✓[/green] Switched to optimal account")

   except NoAccountsAvailable as exc:
      if output_json:
         print(json.dumps({"error": str(exc)}))
      else:
         console.print(f"[red]{exc}[/red]")
         console.print("[yellow]→ Run 'c2switcher ls' to see available accounts[/yellow]")
   except Exception as exc:
      console.print(f"[red]Error: {exc}[/red]")
   finally:
      factory.close()


@click.command()
@click.argument("identifier", required=False)
@click.option("--session-id", help="Session ID for load balancing (when no identifier given)")
@click.option("--token-only", is_flag=True, help="Output only the token to stdout")
@click.option("--with-label", is_flag=True, help="Output label before token (requires --token-only)")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
def switch(identifier: Optional[str], session_id: Optional[str], token_only: bool, with_label: bool, output_json: bool):
   """Switch to a specific account by index, nickname, email, or UUID."""
   if not identifier and not session_id:
      console.print("[red]Error: Must provide either an identifier or --session-id for load balancing[/red]")
      return

   acquire_lock()
   factory = ServiceFactory()

   try:
      switching_service = factory.get_switching_service()

      if identifier:
         # Direct switch by identifier (service already refreshed and persisted credentials)
         account = switching_service.switch_to(identifier, token_only=token_only)
      else:
         # Load-balanced switch with session (service already refreshed and persisted credentials)
         decision = switching_service.select_optimal(
            session_id=session_id,
            token_only=token_only,
            dry_run=False
         )
         account = decision.account

      # Extract token from already-refreshed credentials
      creds = account.get_credentials()
      token = creds.get("claudeAiOauth", {}).get("accessToken")

      if not token:
         if output_json:
            print(json.dumps({"error": "No access token found"}))
         else:
            console.print("[red]Error: No access token found in credentials[/red]")
         return

      if output_json:
         json_output = {
            "index": account.index_num,
            "nickname": account.nickname,
            "email": account.email,
            "switched": not token_only,
         }
         if token_only:
            json_output["token"] = token
         print(json.dumps(json_output, indent=2))
      else:
         nickname = account.nickname or "[dim]none[/dim]"
         masked_email = mask_email(account.email)

         panel_content = (
            f"[green]Switched to account (={account.index_num})[/green]\n\n"
            f"Nickname: [bold]{nickname}[/bold]\n"
            f"Email: [bold]{masked_email}[/bold]"
         )

         if token_only:
            console.print(Panel(panel_content, border_style="green"))
            if with_label:
               print(account.display_identifier())
            print(token)
         else:
            console.print(Panel(panel_content, border_style="green"))

   except NoAccountsAvailable as exc:
      if output_json:
         print(json.dumps({"error": str(exc)}))
      else:
         console.print(f"[red]{exc}[/red]")
         console.print("[yellow]→ Run 'c2switcher ls' to see available accounts[/yellow]")
   except Exception as exc:
      console.print(f"[red]Error: {exc}[/red]")
   finally:
      factory.close()


@click.command()
def cycle():
   """Cycle to the next account in the list."""
   acquire_lock()
   factory = ServiceFactory()

   try:
      account_service = factory.get_account_service()
      accounts = account_service.list_accounts()

      if not accounts:
         console.print("[yellow]No accounts found. Add one with 'c2switcher add'[/yellow]")
         return

      if len(accounts) == 1:
         console.print("[yellow]Only one account available[/yellow]")
         return

      # Find current account
      current_uuid = None
      from ...constants import CREDENTIALS_PATH
      if CREDENTIALS_PATH.exists():
         with open(CREDENTIALS_PATH, "r", encoding="utf-8") as handle:
            try:
               current_creds = json.load(handle)
               current_token = current_creds.get("claudeAiOauth", {}).get("accessToken")
               for acc in accounts:
                  acc_creds = acc.get_credentials()
                  if acc_creds.get("claudeAiOauth", {}).get("accessToken") == current_token:
                     current_uuid = acc.uuid
                     break
            except Exception:
               pass

      # Find next account
      if current_uuid:
         current_index = None
         for idx, acc in enumerate(accounts):
            if acc.uuid == current_uuid:
               current_index = idx
               break
         next_account = accounts[(current_index + 1) % len(accounts)] if current_index is not None else accounts[0]
      else:
         next_account = accounts[0]

      # Switch to next account
      credential_store = factory.get_credential_store()
      refreshed_creds = credential_store.refresh_access_token(next_account.credentials_json)
      credential_store.write_credentials(refreshed_creds)

      console.print(
         Panel(
            f"[green]Switched to next account:[/green] {next_account.nickname or next_account.email}",
            border_style="green",
         )
      )

   finally:
      factory.close()
