"""Shared constants for the c2switcher package."""

from pathlib import Path

from .presentation.console import console

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
CACHE_TTL_SECONDS = 300  # accept cache up to this age (5 minutes)
STALE_CACHE_SECONDS = 60  # refresh stale cache if needed (for high-drain accounts)
HIGH_DRAIN_REFRESH_THRESHOLD = 1.0  # %/hour that warrants a fresh usage pull
FIVE_HOUR_PENALTIES = [
   (90.0, 0.5),
   (85.0, 0.7),
   (80.0, 0.85),
]
FIVE_HOUR_ROTATION_CAP = 90.0  # avoid round robin entries above this 5h util
BURST_THRESHOLD = 94.0  # skip accounts whose expected burst would exceed this
DEFAULT_BURST_BUFFER = 4.0  # fallback burst size when history is sparse
LOW_USAGE_BONUS_CAP = 60.0  # upper bound for low-util bonus to taper
LOW_USAGE_BONUS_FLOOR = 20.0  # treat anything below as min util for bonus math
LOW_USAGE_BONUS_GAIN = 5.0  # max %/hour boost awarded to low-util accounts
OPUS_BONUS_THRESHOLD = 85.0  # opus under this keeps bonus enabled
OPUS_PACE_GATE = 90.0  # enable pace alignment once opus is hot
OPUS_PENALTY_THRESHOLD = 95.0  # opus over this incurs penalty
OPUS_HIGH_UTIL_PENALTY = 2.0  # fixed %/hour penalty for overheated opus
