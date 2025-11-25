"""Command-line interface for c2switcher."""

import click

from .accounts import add, list_accounts_cmd, current, force_refresh
from .switching import optimal, switch, cycle
from .sessions_cmd import start_session_cmd, end_session, list_sessions, session_history
from .reports import report_sessions, report_usage
from .usage_cmd import usage
from .login import login
from .apikey import apikey


@click.group()
def cli():
    """Claude Code Account Switcher - Manage multiple Claude Code accounts."""


# Register commands
cli.add_command(login)
cli.add_command(add)
cli.add_command(list_accounts_cmd)
cli.add_command(current)
cli.add_command(force_refresh)

cli.add_command(optimal)
cli.add_command(switch)
cli.add_command(cycle)

cli.add_command(start_session_cmd)
cli.add_command(end_session)
cli.add_command(list_sessions)
cli.add_command(session_history)

cli.add_command(report_sessions)
cli.add_command(report_usage)

cli.add_command(usage)

cli.add_command(apikey)


# Aliases
@cli.command(name='history', hidden=True)
@click.pass_context
def history_alias(ctx):
    """Alias for 'session-history'."""
    ctx.forward(session_history)


@cli.command(name='list', hidden=True)
@click.pass_context
def list_alias(ctx):
    """Alias for 'ls'."""
    ctx.forward(list_accounts_cmd)


@cli.command(name='list-accounts', hidden=True)
@click.pass_context
def list_accounts_alias(ctx):
    """Alias for 'ls'."""
    ctx.forward(list_accounts_cmd)


@cli.command(name='pick', hidden=True)
@click.pass_context
def pick(ctx):
    """Alias for 'optimal'."""
    ctx.forward(optimal)


@cli.command(name='use', hidden=True)
@click.pass_context
def use(ctx):
    """Alias for 'switch'."""
    ctx.forward(switch)


__all__ = ['cli']
