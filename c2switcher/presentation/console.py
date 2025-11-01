"""Shared console instance for c2switcher output.

This module provides a single Rich Console instance configured to write to stderr.
Using stderr ensures output doesn't interfere with stdout-based integrations.
"""

from rich.console import Console

console = Console(stderr=True)
