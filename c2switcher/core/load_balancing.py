"""Pure load balancing logic for account selection."""

from __future__ import annotations

from typing import Dict, List, Optional

from .models import Account, Candidate, UsageSnapshot

# Load balancer tuning parameters (imported from constants in real usage)
SIMILAR_DRAIN_THRESHOLD = 0.05
FIVE_HOUR_PENALTIES = [
   (90.0, 0.5),
   (85.0, 0.7),
   (80.0, 0.85),
]
FIVE_HOUR_ROTATION_CAP = 90.0
BURST_THRESHOLD = 94.0
FRESH_UTILIZATION_THRESHOLD = 25.0
FRESH_ACCOUNT_MAX_BONUS = 3.0
WINDOW_LENGTH_HOURS = 168.0
PACE_GAIN = 1.0
PACE_AHEAD_DAMPING = 0.5
MAX_PACE_ADJUSTMENT = 4.0


def build_candidate(
   account: Account,
   usage: UsageSnapshot,
   burst_buffer: float,
   active_sessions: int,
   recent_sessions: int,
   *,
   refreshed: bool = False,
) -> Optional[Candidate]:
   """
   Score account for load balancing.

   Returns None if account is fully exhausted (99%+ on all windows).
   """
   opus_util_raw = usage.seven_day_opus.utilization
   overall_util_raw = usage.seven_day.utilization

   opus_util = float(opus_util_raw) if opus_util_raw is not None else 100.0
   overall_util = float(overall_util_raw) if overall_util_raw is not None else 100.0

   # Exhausted on both windows
   if opus_util >= 99.0 and overall_util >= 99.0:
      return None

   # Select window: prefer opus tier if available
   if opus_util < 99.0:
      window = "opus"
      tier = 1
      utilization = opus_util
      hours_to_reset = usage.seven_day_opus.hours_until_reset()
   else:
      window = "overall"
      tier = 2
      utilization = overall_util
      hours_to_reset = usage.seven_day.hours_until_reset()

   # Core metrics
   headroom = max(99.0 - utilization, 0.0)
   effective_hours_left = max(hours_to_reset, 0.001)
   drain_rate = headroom / effective_hours_left if headroom > 0 else 0.0

   # Pace alignment: how far ahead/behind of schedule this account is
   window_hours = WINDOW_LENGTH_HOURS
   elapsed_hours = max(window_hours - min(hours_to_reset, window_hours), 0.0)
   expected_utilization = (elapsed_hours / window_hours) * 100.0
   expected_utilization = max(0.0, min(expected_utilization, 100.0))
   pace_gap = expected_utilization - utilization
   pace_adjustment = 0.0
   if headroom > 0:
      pace_adjustment = (pace_gap / effective_hours_left) * PACE_GAIN
      if pace_gap < 0:
         pace_adjustment *= PACE_AHEAD_DAMPING
      pace_adjustment = max(min(pace_adjustment, MAX_PACE_ADJUSTMENT), -MAX_PACE_ADJUSTMENT)

   # Fresh account bonus
   fresh_bonus = 0.0
   if headroom > 0 and utilization < FRESH_UTILIZATION_THRESHOLD and pace_gap > 0:
      freshness = (FRESH_UTILIZATION_THRESHOLD - utilization) / FRESH_UTILIZATION_THRESHOLD
      fresh_bonus = freshness * FRESH_ACCOUNT_MAX_BONUS

   priority_drain = drain_rate + pace_adjustment + fresh_bonus

   # 5-hour penalty
   five_hour_util_raw = usage.five_hour.utilization
   five_hour_util = float(five_hour_util_raw) if five_hour_util_raw is not None else 50.0

   five_hour_factor = 1.0
   for threshold, factor in FIVE_HOUR_PENALTIES:
      if five_hour_util >= threshold:
         five_hour_factor = factor
         break

   adjusted_drain = priority_drain * five_hour_factor

   # Burst blocking
   expected_burst = burst_buffer
   burst_blocked = (utilization + expected_burst) >= BURST_THRESHOLD

   return Candidate(
      account=account,
      usage=usage,
      tier=tier,
      window=window,
      utilization=utilization,
      headroom=headroom,
      hours_to_reset=hours_to_reset,
      drain_rate=drain_rate,
      expected_utilization=expected_utilization,
      pace_gap=pace_gap,
      pace_adjustment=pace_adjustment,
      fresh_bonus=fresh_bonus,
      priority_drain=priority_drain,
      five_hour_utilization=five_hour_util,
      five_hour_factor=five_hour_factor,
      adjusted_drain=adjusted_drain,
      expected_burst=expected_burst,
      burst_blocked=burst_blocked,
      active_sessions=active_sessions,
      recent_sessions=recent_sessions,
      refreshed=refreshed,
   )


def select_best_candidate(candidates: List[Candidate]) -> Optional[Candidate]:
   """
   Select optimal candidate from scored list.

   Applies burst blocking, 5-hour filtering, and rank-based selection.
   Returns None if no suitable candidates.
   """
   if not candidates:
      return None

   # Prefer non-burst-blocked
   usable = [c for c in candidates if not c.burst_blocked]
   pool = usable if usable else candidates

   # Prefer cool 5-hour utilization
   cool = [c for c in pool if c.five_hour_utilization < FIVE_HOUR_ROTATION_CAP]
   pool = cool if cool else pool

   # Sort by rank (descending)
   pool.sort(key=lambda c: c.rank, reverse=True)

   return pool[0]


def select_top_similar_candidates(candidates: List[Candidate], threshold: float = SIMILAR_DRAIN_THRESHOLD) -> List[Candidate]:
   """
   Group candidates with similar adjusted drain rates.

   Returns all candidates within threshold of the top candidate.
   Used for round-robin among equally good choices.
   """
   if not candidates:
      return []

   candidates_sorted = sorted(candidates, key=lambda c: c.rank, reverse=True)
   top = candidates_sorted[0]

   similar = [
      c
      for c in candidates_sorted
      if c.tier == top.tier and abs(top.adjusted_drain - c.adjusted_drain) <= threshold
   ]

   return similar


def needs_refresh(candidate: Candidate, stale_seconds: float = 60.0, high_drain_threshold: float = 1.0) -> bool:
   """
   Determine if candidate's usage cache should be refreshed.

   Refresh if:
   - Cache is stale (>60s) OR
   - High drain (>1.0 %/h) with cache >10s old
   """
   if candidate.refreshed:
      return False

   if candidate.usage.cache_source == "live":
      return False

   cache_age = candidate.usage.cache_age_seconds

   if cache_age > stale_seconds:
      return True

   if candidate.priority_drain >= high_drain_threshold and cache_age > 10:
      return True

   return False
