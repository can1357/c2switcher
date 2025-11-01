"""Shared constants for the c2switcher package."""

from pathlib import Path

from rich.console import Console

# Single shared console instance for consistent styling
console = Console(stderr=True)

# Paths
C2SWITCHER_DIR = Path.home() / ".c2switcher"
DB_PATH = C2SWITCHER_DIR / "store.db"
LOCK_PATH = C2SWITCHER_DIR / ".lock"
HEADERS_PATH = C2SWITCHER_DIR / "headers.json"
CLAUDE_DIR = Path.home() / ".claude"
CREDENTIALS_PATH = CLAUDE_DIR / ".credentials.json"
LB_STATE_PATH = C2SWITCHER_DIR / "load_balancer_state.json"

# Load balancer tuning parameters
SIMILAR_DRAIN_THRESHOLD = 0.05  # %/hour margin to consider accounts interchangeable
STALE_CACHE_SECONDS = 60  # force refresh if cache older than this (seconds)
HIGH_DRAIN_REFRESH_THRESHOLD = 1.0  # %/hour that warrants a fresh usage pull
FIVE_HOUR_PENALTIES = [
    (90.0, 0.5),
    (85.0, 0.7),
    (80.0, 0.85),
]
FIVE_HOUR_ROTATION_CAP = 90.0  # avoid round robin entries above this 5h util
BURST_THRESHOLD = 94.0  # skip accounts whose expected burst would exceed this
DEFAULT_BURST_BUFFER = 4.0  # fallback burst size when history is sparse
FRESH_UTILIZATION_THRESHOLD = 25.0  # % usage considered "fresh" and boosted
FRESH_ACCOUNT_MAX_BONUS = 3.0  # max %/hour boost applied to very fresh accounts
