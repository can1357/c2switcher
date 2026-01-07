"""Usage reporting commands."""

from __future__ import annotations

import json
from datetime import datetime

import click
from rich import box
from rich.table import Table

from ...constants import console
from ...infrastructure.locking import acquire_lock
from ...infrastructure.factory import ServiceFactory
from ...infrastructure.api import ClaudeAPI
from ...utils import format_time_until_reset, parse_sqlite_timestamp_to_local


def _get_account_usage(store, account_uuid: str, credentials_json: str, force: bool = False):
    """Fetch usage for account with caching."""
    from datetime import timezone

    if not force:
        cached = store.get_recent_usage(account_uuid, max_age_seconds=300)
        if cached:
            cache_age = None
            try:
                cache_dt = datetime.fromisoformat(cached.queried_at.replace('Z', '+00:00'))
                cache_age = max((datetime.now(timezone.utc) - cache_dt).total_seconds(), 0)
            except Exception:
                cache_age = None

            return {
                'five_hour': {
                    'utilization': cached.five_hour.utilization,
                },
                'seven_day': {
                    'utilization': cached.seven_day.utilization,
                    'resets_at': cached.seven_day.resets_at,
                },
                'seven_day_opus': {
                    'utilization': cached.seven_day_opus.utilization,
                    'resets_at': cached.seven_day_opus.resets_at,
                },
                'seven_day_sonnet': {
                    'utilization': cached.seven_day_sonnet.utilization,
                    'resets_at': cached.seven_day_sonnet.resets_at,
                },
                '_cache_source': 'cache',
                '_cache_age_seconds': cache_age,
                '_queried_at': cached.queried_at,
            }

    # Fetch fresh usage
    from ...data.credential_store import CredentialStore
    from ...constants import CREDENTIALS_PATH

    cred_store = CredentialStore(CREDENTIALS_PATH)
    refreshed_creds = cred_store.refresh_access_token(credentials_json)
    token = refreshed_creds.get('claudeAiOauth', {}).get('accessToken')

    if not token:
        raise ValueError('No access token found in credentials')

    usage = ClaudeAPI.get_usage(token)

    # Check if API returned all nulls (intermittent API bug)
    has_data = any([
        usage.get('five_hour'),
        usage.get('seven_day'),
        usage.get('seven_day_sonnet'),
    ])

    if not has_data:
        # Fall back to cached data (up to 24h old)
        cached = store.get_recent_usage(account_uuid, max_age_seconds=86400, require_data=True)
        if cached:
            cache_age = None
            try:
                cache_dt = datetime.fromisoformat(cached.queried_at.replace('Z', '+00:00'))
                cache_age = max((datetime.now(timezone.utc) - cache_dt).total_seconds(), 0)
            except Exception:
                cache_age = None

            return {
                'five_hour': {'utilization': cached.five_hour.utilization},
                'seven_day': {
                    'utilization': cached.seven_day.utilization,
                    'resets_at': cached.seven_day.resets_at,
                },
                'seven_day_opus': {
                    'utilization': cached.seven_day_opus.utilization,
                    'resets_at': cached.seven_day_opus.resets_at,
                },
                'seven_day_sonnet': {
                    'utilization': cached.seven_day_sonnet.utilization,
                    'resets_at': cached.seven_day_sonnet.resets_at,
                },
                '_cache_source': 'fallback',
                '_cache_age_seconds': cache_age,
                '_queried_at': cached.queried_at,
            }

    usage['_cache_source'] = 'live'
    usage['_cache_age_seconds'] = 0.0
    usage['_queried_at'] = datetime.now(timezone.utc).isoformat()

    # Save to DB (only if we have actual data)
    store.save_usage(account_uuid, usage)

    # Update credentials if changed
    if refreshed_creds != json.loads(credentials_json):
        store.update_credentials(account_uuid, refreshed_creds)

    return usage


@click.command()
@click.option('--json', 'output_json', is_flag=True, help='Output as JSON')
@click.option('--force', is_flag=True, help='Force refresh (ignore cache)')
def usage(output_json: bool, force: bool):
    """List usage across all accounts with session distribution."""
    acquire_lock()
    factory = ServiceFactory()

    try:
        session_service = factory.get_session_service()
        session_service.maybe_cleanup()

        account_service = factory.get_account_service()
        accounts = account_service.list_accounts()

        if not accounts:
            console.print("[yellow]No accounts found. Add one with 'c2switcher add'[/yellow]")
            return

        store = factory.get_store()
        session_counts = store.get_active_session_counts()

        usage_data = []
        for acc in accounts:
            try:
                display_name = acc.nickname or acc.email
                with console.status(f'[bold green]Fetching usage for {display_name}...'):
                    usage_info = _get_account_usage(store, acc.uuid, acc.credentials_json, force=force)

                usage_data.append(
                    {
                        'account': acc,
                        'usage': usage_info,
                        'sessions': session_counts.get(acc.uuid, 0),
                    }
                )
            except Exception as exc:
                display_name = acc.nickname or acc.email
                console.print(f'[red]Error fetching usage for {display_name}: {exc}[/red]')
                usage_data.append(
                    {
                        'account': acc,
                        'usage': None,
                        'sessions': session_counts.get(acc.uuid, 0),
                        'error': str(exc),
                    }
                )

        if output_json:
            result = []
            for item in usage_data:
                acc = item['account']
                result.append(
                    {
                        'index': acc.index_num,
                        'nickname': acc.nickname,
                        'email': acc.email,
                        'usage': item['usage'],
                        'sessions': item['sessions'],
                        'error': item.get('error'),
                    }
                )
            print(json.dumps(result, indent=2))
            return

        table = Table(title='Usage Across Accounts', box=box.ROUNDED)
        table.add_column('Index', style='cyan', justify='center')
        table.add_column('Nickname', style='magenta')
        table.add_column('Email', style='green')
        table.add_column('5h', justify='right')
        table.add_column('7d', justify='right')
        table.add_column('7d Sonnet', justify='right')
        table.add_column('Reset (Rate)', justify='right', no_wrap=True)
        table.add_column('Sessions', style='blue', justify='center')

        for item in usage_data:
            acc = item['account']
            usage_info = item['usage']
            sessions = item['sessions']

            session_str = f'[blue]{sessions}[/blue]' if sessions > 0 else '[dim]0[/dim]'

            if usage_info is None:
                table.add_row(
                    str(acc.index_num),
                    acc.nickname or '[dim]--[/dim]',
                    acc.email,
                    '[red]Error[/red]',
                    '[red]Error[/red]',
                    '[red]Error[/red]',
                    '[red]Error[/red]',
                    session_str,
                )
                continue

            five_hour = usage_info.get('five_hour', {}) or {}
            seven_day = usage_info.get('seven_day', {}) or {}
            seven_day_sonnet = usage_info.get('seven_day_sonnet', {}) or {}

            def format_usage(value):
                if value is None:
                    return '[dim]--[/dim]'
                if value >= 90:
                    return f'[red]{value}%[/red]'
                if value >= 70:
                    return f'[yellow]{value}%[/yellow]'
                return f'[green]{value}%[/green]'

            sonnet_util = seven_day_sonnet.get('utilization')
            overall_util = seven_day.get('utilization')
            reset_time = format_time_until_reset(
                seven_day_sonnet.get('resets_at') if seven_day_sonnet else None,
                seven_day.get('resets_at'),
                sonnet_util if sonnet_util is not None else 0,
                overall_util if overall_util is not None else 0,
            )

            table.add_row(
                str(acc.index_num),
                acc.nickname or '[dim]--[/dim]',
                acc.email,
                format_usage(five_hour.get('utilization')),
                format_usage(seven_day.get('utilization')),
                format_usage(seven_day_sonnet.get('utilization')),
                reset_time,
                session_str,
            )

        console.print(table)

        # Show active sessions
        active_sessions = session_service.list_active()
        if active_sessions:
            console.print(f'\n[bold]Active Sessions ({len(active_sessions)}):[/bold]')
            for session in active_sessions[:5]:
                account_email = '[dim]not assigned[/dim]'
                if session.account_uuid:
                    acc = store.get_account_by_identifier(session.account_uuid)
                    if acc:
                        account_email = acc.email

                started_dt = parse_sqlite_timestamp_to_local(session.created_at)

                time_ago = datetime.now() - started_dt
                if time_ago.total_seconds() < 60:
                    time_str = f'{int(time_ago.total_seconds())}s ago'
                elif time_ago.total_seconds() < 3600:
                    time_str = f'{int(time_ago.total_seconds() / 60)}m ago'
                else:
                    time_str = f'{int(time_ago.total_seconds() / 3600)}h ago'

                cwd = session.cwd or 'unknown'
                if len(cwd) > 35:
                    cwd = '...' + cwd[-32:]

                console.print(f'  * {account_email} [dim]({cwd}, {time_str})[/dim]')

            if len(active_sessions) > 5:
                console.print(f'  [dim]... and {len(active_sessions) - 5} more[/dim]')

    finally:
        factory.close()
