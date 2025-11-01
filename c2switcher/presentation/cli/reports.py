"""Reporting commands."""

from __future__ import annotations

from pathlib import Path

import click

from ...constants import C2SWITCHER_DIR


DEFAULT_DB_PATH = C2SWITCHER_DIR / "store.db"
DEFAULT_SESSION_OUTPUT = Path.home() / "c2switcher_session_report.png"
DEFAULT_USAGE_OUTPUT = Path.home() / "c2switcher_usage_report.png"


@click.command(name="report-sessions")
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
   from ...reports.sessions import generate_session_report

   generate_session_report(db_path, output_path, days=days, min_duration=min_duration, show=show)


@click.command(name="report-usage")
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
   from ...reports.usage import generate_usage_report

   generate_usage_report(db_path, output_path, window_hours=window_hours, show=show)
