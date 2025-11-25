"""Login command for OAuth authentication."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import click

from ...constants import CREDENTIALS_PATH, console
from ...infrastructure.oauth import OAuthClient
from ...infrastructure.locking import acquire_lock


@click.command(name='login')
@click.option(
    '--output',
    '-o',
    type=click.Path(),
    help='Output path (default: ~/.claude/.credentials.json)',
)
@click.option('--no-browser', is_flag=True, help="Don't auto-open browser")
@click.option(
    '--manual-only',
    is_flag=True,
    help='Use manual code paste (disable automatic localhost callback)',
)
@click.option('--no-add', is_flag=True, help="Don't add account to c2switcher after login")
@click.option('--nickname', '-n', help='Nickname for the account')
def login(
    output: Optional[str],
    no_browser: bool,
    manual_only: bool,
    no_add: bool,
    nickname: Optional[str],
):
    """Authenticate with Anthropic and generate credentials.json.

    By default uses dual flow: automatic localhost callback + manual fallback.
    Use --manual-only to disable localhost server and only use copy/paste.
    """

    output_path = Path(output) if output else CREDENTIALS_PATH

    console.print('[bold cyan]Claude Code OAuth Login[/bold cyan]\n')

    if manual_only:
        console.print('[dim]Using manual-only flow (copy/paste code)[/dim]')
    else:
        console.print('[dim]Using dual flow (automatic + manual fallback)[/dim]')

    console.print(f'Credentials will be saved to: [yellow]{output_path}[/yellow]\n')

    try:
        client = OAuthClient()
        credentials = client.login(auto_open=not no_browser, use_dual_flow=not manual_only)

        # Write credentials
        output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

        temp_path = output_path.with_suffix('.tmp')
        try:
            with temp_path.open('w', encoding='utf-8') as f:
                json.dump(credentials, f, indent=2)

            import os

            os.chmod(temp_path, 0o600)
            temp_path.replace(output_path)
        except Exception:
            if temp_path.exists():
                temp_path.unlink()
            raise

        console.print(f'\n[green]✓[/green] Credentials saved to [yellow]{output_path}[/yellow]\n')

        # Add to c2switcher automatically (unless --no-add)
        if not no_add:
            acquire_lock()
            from ...infrastructure.factory import ServiceFactory

            factory = ServiceFactory()
            try:
                with output_path.open('r', encoding='utf-8') as f:
                    credentials_json = f.read()

                account_service = factory.get_account_service()
                account, is_new = account_service.add_account(credentials_json, nickname=nickname)

                console.print(
                    f'[green]✓[/green] Account {"added" if is_new else "updated"}\n'
                    f'  Index: [bold]{account.index_num}[/bold]\n'
                    f'  Email: [bold]{account.email}[/bold]\n'
                    f'  Name: {account.display_name or account.full_name}\n'
                )
            except Exception as exc:
                console.print(f'[yellow]Warning: Failed to add account: {exc}[/yellow]')
            finally:
                factory.close()
        else:
            console.print('[dim]Account saved but not added to c2switcher (--no-add)[/dim]')

    except KeyboardInterrupt:
        console.print('\n[yellow]Login cancelled[/yellow]')
    except Exception as exc:
        console.print(f'\n[red]Login failed: {exc}[/red]')
        raise click.Abort()
