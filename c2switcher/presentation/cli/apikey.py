"""API key management commands."""

from __future__ import annotations

from typing import Optional, Tuple

import click
import requests
from rich.panel import Panel
from rich.table import Table

from ...constants import console
from ...infrastructure.locking import acquire_lock
from ...infrastructure.factory import ServiceFactory
from ...utils import mask_email


def mask_api_key(key: str) -> str:
    """Mask API key for display, showing only prefix and suffix."""
    if len(key) <= 12:
        return key[:4] + '***'
    return key[:8] + '...' + key[-4:]


def probe_api_key(api_key: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Probe an API key to get the organization ID.

    Returns (org_uuid, error_message).
    """
    try:
        resp = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'Authorization': f'Bearer {api_key}',
                'anthropic-version': '2023-06-01',
                'anthropic-beta': 'claude-code-20250219,oauth-2025-04-20',
                'anthropic-dangerous-direct-browser-access': 'true',
                'content-type': 'application/json',
                'user-agent': 'claude-code/2.0.46',
                'x-app': 'cli',
            },
            json={
                'model': 'claude-3-haiku-20240307',
                'max_tokens': 1,
                'system': "You are Claude Code, Anthropic's official CLI for Claude.",
                'messages': [{'role': 'user', 'content': 'hi'}],
            },
            timeout=(5, 20),
        )

        if resp.status_code == 200:
            org_uuid = resp.headers.get('anthropic-organization-id')
            return org_uuid, None
        else:
            error = resp.json().get('error', {}).get('message', 'Unknown error')
            return None, error

    except requests.RequestException as e:
        return None, str(e)


@click.group()
def apikey():
    """Manage long-lived API keys for accounts.

    Long-lived API keys (1-year tokens) are used for Claude Code authentication
    instead of ephemeral OAuth tokens. This prevents mid-session token revocation.

    The OAuth token is still used internally for usage queries and refresh.
    """


@apikey.command(name='add')
@click.argument('key', required=False)
@click.option('--stdin', is_flag=True, help='Read API key from stdin')
def add_apikey(key: Optional[str], stdin: bool):
    """Add a long-lived API key (auto-assigns to matching account).

    Probes the key to determine which account it belongs to based on
    organization ID, then assigns it automatically.
    """
    acquire_lock()
    factory = ServiceFactory()

    try:
        store = factory.get_store()

        # Get the API key
        if key:
            api_key = key.strip()
        elif stdin:
            import sys

            api_key = sys.stdin.read().strip()
        else:
            api_key = click.prompt('Enter API key', hide_input=True).strip()

        if not api_key:
            console.print('[red]Error: API key cannot be empty[/red]')
            return

        if not api_key.startswith('sk-ant-'):
            console.print("[yellow]Warning: API key does not start with 'sk-ant-'[/yellow]")
            if not click.confirm('Continue anyway?'):
                return

        console.print('[dim]Probing API key...[/dim]')
        org_uuid, error = probe_api_key(api_key)

        if error:
            console.print(f'[red]Error probing API key: {error}[/red]')
            return

        if not org_uuid:
            console.print('[red]Error: Could not determine organization ID from API key[/red]')
            return

        # Find matching account
        accounts = store.list_accounts()
        matching_account = None
        for acc in accounts:
            if acc.org_uuid == org_uuid:
                matching_account = acc
                break

        if not matching_account:
            console.print(f'[red]No account found with organization ID: {org_uuid}[/red]')
            console.print("[yellow]Make sure the account is registered with 'c2switcher add' first[/yellow]")
            return

        store.set_api_key(matching_account.uuid, api_key)

        nickname = matching_account.nickname or '[dim]none[/dim]'
        masked_email = mask_email(matching_account.email)

        console.print(
            Panel(
                f'[green]API key added for account (={matching_account.index_num})[/green]\n\n'
                f'Nickname: [bold]{nickname}[/bold]\n'
                f'Email: [bold]{masked_email}[/bold]\n'
                f'API Key: [cyan]{mask_api_key(api_key)}[/cyan]',
                border_style='green',
            )
        )

    finally:
        factory.close()


@apikey.command(name='clear')
@click.argument('identifier')
@click.option('--force', '-f', is_flag=True, help='Skip confirmation')
def clear_apikey(identifier: str, force: bool):
    """Clear long-lived API key for an account.

    After clearing, the account will use OAuth tokens for Claude Code.
    """
    acquire_lock()
    factory = ServiceFactory()

    try:
        store = factory.get_store()
        account = store.get_account_by_identifier(identifier)

        if not account:
            console.print(f'[red]Account not found: {identifier}[/red]')
            return

        if not account.api_key:
            console.print(f'[yellow]Account {account.display_identifier()} has no API key set[/yellow]')
            return

        if not force:
            if not click.confirm(f'Clear API key for {account.display_identifier()}?'):
                return

        store.set_api_key(account.uuid, None)

        console.print(
            Panel(
                f'[green]API key cleared for account (={account.index_num})[/green]\n\n'
                f'Account: [bold]{account.display_identifier()}[/bold]\n'
                f'[dim]OAuth tokens will now be used for Claude Code[/dim]',
                border_style='green',
            )
        )

    finally:
        factory.close()


@apikey.command(name='show')
@click.argument('identifier', required=False)
def show_apikey(identifier: Optional[str]):
    """Show API key status for one or all accounts.

    Without IDENTIFIER, shows status for all accounts.
    """
    acquire_lock()
    factory = ServiceFactory()

    try:
        store = factory.get_store()

        if identifier:
            account = store.get_account_by_identifier(identifier)
            if not account:
                console.print(f'[red]Account not found: {identifier}[/red]')
                return

            nickname = account.nickname or '[dim]none[/dim]'
            masked_email = mask_email(account.email)

            if account.api_key:
                key_status = f'[green]Set[/green] ({mask_api_key(account.api_key)})'
            else:
                key_status = '[yellow]Not set[/yellow] (using OAuth)'

            console.print(
                Panel(
                    f'Account (={account.index_num})\n\n'
                    f'Nickname: [bold]{nickname}[/bold]\n'
                    f'Email: [bold]{masked_email}[/bold]\n'
                    f'API Key: {key_status}',
                    border_style='blue',
                )
            )
        else:
            accounts = store.list_accounts()
            if not accounts:
                console.print('[yellow]No accounts registered[/yellow]')
                return

            table = Table(title='API Key Status')
            table.add_column('=', style='dim')
            table.add_column('Account')
            table.add_column('API Key')

            for acc in accounts:
                display = acc.nickname or mask_email(acc.email)
                if acc.api_key:
                    key_status = f'[green]✓[/green] {mask_api_key(acc.api_key)}'
                else:
                    key_status = '[dim]OAuth[/dim]'
                table.add_row(str(acc.index_num), display, key_status)

            console.print(table)

    finally:
        factory.close()
