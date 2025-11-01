"""Account management commands."""

from __future__ import annotations

import json
from typing import Optional

import click
from rich import box
from rich.panel import Panel
from rich.table import Table

from ...constants import CREDENTIALS_PATH, console
from ...infrastructure.locking import acquire_lock
from ...infrastructure.factory import ServiceFactory
from ...core.errors import AccountNotFound, InvalidCredentials, ProfileFetchError
from ...utils import mask_email


@click.command()
@click.option("--nickname", "-n", help="Optional nickname for the account")
@click.option("--creds-file", "-f", type=click.Path(exists=True), help="Path to credentials JSON file")
def add(nickname: Optional[str], creds_file: Optional[str]):
   """Add a new account from credentials file or current .credentials.json."""
   acquire_lock()
   factory = ServiceFactory()

   try:
      if creds_file:
         with open(creds_file, "r", encoding="utf-8") as handle:
            credentials_json = handle.read()
      else:
         if not CREDENTIALS_PATH.exists():
            console.print(f"[red]Error: {CREDENTIALS_PATH} not found[/red]")
            console.print("[yellow]Please specify a credentials file with --creds-file[/yellow]")
            return
         with open(CREDENTIALS_PATH, "r", encoding="utf-8") as handle:
            credentials_json = handle.read()

      account_service = factory.get_account_service()
      account, is_new = account_service.add_account(credentials_json, nickname=nickname)

      console.print(
         Panel(
            f"[green]✓[/green] Account {'added' if is_new else 'updated'} successfully\n\n"
            f"Index: [bold]{account.index_num}[/bold]\n"
            f"Email: [bold]{account.email}[/bold]\n"
            f"Name: {account.display_name or account.full_name}\n"
            f"Nickname: {nickname or '[dim]none[/dim]'}",
            title="Account Added" if is_new else "Account Updated",
            border_style="green",
         )
      )

   except (InvalidCredentials, ProfileFetchError) as exc:
      console.print(f"[red]Error: {exc}[/red]")
   except Exception as exc:
      console.print(f"[red]Error adding account: {exc}[/red]")
   finally:
      factory.close()


@click.command(name="ls")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
def list_accounts_cmd(output_json: bool):
   """List all accounts."""
   factory = ServiceFactory()

   try:
      account_service = factory.get_account_service()
      accounts = account_service.list_accounts()

      if output_json:
         result = []
         for acc in accounts:
            result.append(
               {
                  "index": acc.index_num,
                  "nickname": acc.nickname,
                  "email": acc.email,
                  "full_name": acc.full_name,
                  "display_name": acc.display_name,
                  "has_claude_max": acc.has_claude_max,
                  "has_claude_pro": acc.has_claude_pro,
                  "org_type": acc.org_type,
                  "rate_limit_tier": acc.rate_limit_tier,
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

      console.print(table)

   finally:
      factory.close()


@click.command(name="current")
@click.option("--format", type=click.Choice(["default", "prompt"]), default="default", help="Output format (default, prompt)")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
def current(format: str, output_json: bool):
   """Show currently selected account from credentials file."""
   if not CREDENTIALS_PATH.exists():
      if output_json:
         print(json.dumps({"error": "No credentials file found"}))
      else:
         console.print("[yellow]No credentials file found[/yellow]")
         console.print("[yellow]→ Run 'c2switcher optimal' or 'c2switcher switch <account>' to select an account[/yellow]")
      return

   factory = ServiceFactory()

   try:
      with open(CREDENTIALS_PATH, "r", encoding="utf-8") as handle:
         current_creds = json.load(handle)

      current_token = current_creds.get("claudeAiOauth", {}).get("accessToken")
      if not current_token:
         if output_json:
            print(json.dumps({"error": "No access token in credentials file"}))
         else:
            console.print("[yellow]No access token in credentials file[/yellow]")
         return

      account_service = factory.get_account_service()
      accounts = account_service.list_accounts()
      current_account = None

      for acc in accounts:
         acc_creds = acc.get_credentials()
         acc_token = acc_creds.get("claudeAiOauth", {}).get("accessToken")
         if acc_token == current_token:
            current_account = acc
            break

      if not current_account:
         if output_json:
            print(json.dumps({"error": "Current account not found in database"}))
         else:
            console.print("[yellow]Current account not found in database[/yellow]")
            console.print("[yellow]→ Run 'c2switcher add' to add this account[/yellow]")
         return

      if output_json:
         print(json.dumps({
            "index": current_account.index_num,
            "nickname": current_account.nickname,
            "email": current_account.email,
            "full_name": current_account.full_name,
            "display_name": current_account.display_name,
         }, indent=2))
      elif format == "prompt":
         nickname = current_account.nickname or current_account.email.split("@")[0]
         print(f"[{current_account.index_num}] {nickname}")
      else:
         nickname = current_account.nickname or "[dim]none[/dim]"
         masked_email = mask_email(current_account.email)
         console.print(
            Panel(
               f"[green]Current Account (={current_account.index_num})[/green]\n\n"
               f"Nickname: [bold]{nickname}[/bold]\n"
               f"Email: [bold]{masked_email}[/bold]\n"
               f"Name: {current_account.display_name or current_account.full_name or '[dim]--[/dim]'}",
               border_style="green",
            )
         )

   except Exception as exc:
      if output_json:
         print(json.dumps({"error": str(exc)}))
      else:
         console.print(f"[red]Error: {exc}[/red]")
   finally:
      factory.close()


@click.command(name="force-refresh")
@click.argument("identifier", required=False)
def force_refresh(identifier: Optional[str]):
   """Force refresh tokens for an account (or all accounts if none specified)."""
   acquire_lock()
   factory = ServiceFactory()

   try:
      account_service = factory.get_account_service()
      credential_store = factory.get_credential_store()

      if identifier:
         try:
            account = account_service.get_account(identifier)
            accounts_to_refresh = [account]
         except AccountNotFound:
            console.print(f"[red]Account not found: {identifier}[/red]")
            console.print("[yellow]→ Run 'c2switcher ls' to see available accounts[/yellow]")
            return
      else:
         accounts_to_refresh = account_service.list_accounts()

      if not accounts_to_refresh:
         console.print("[yellow]No accounts to refresh[/yellow]")
         return

      console.print(f"[yellow]Force refreshing {len(accounts_to_refresh)} account(s)...[/yellow]\n")

      for account in accounts_to_refresh:
         account_display = f"[{account.index_num}] {account.nickname or account.email}"

         try:
            refreshed_creds = credential_store.refresh_access_token(account.credentials_json, force=True)

            # Update stored credentials
            factory.get_store().update_credentials(account.uuid, refreshed_creds)

            expires_at = refreshed_creds.get("claudeAiOauth", {}).get("expiresAt", 0)
            import time
            expires_in_hours = (expires_at - int(time.time() * 1000)) / 1000 / 3600

            console.print(f"[green]✓[/green] {account_display} - expires in {expires_in_hours:.1f}h")

         except Exception as exc:
            console.print(f"[red]✗[/red] {account_display} - Error: {exc}")

   except Exception as exc:
      console.print(f"[red]Error: {exc}[/red]")
   finally:
      factory.close()
