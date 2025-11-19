"""Pure load balancing logic for account selection."""

from __future__ import annotations

from typing import Dict, List, Optional

from ..constants import (
   BURST_THRESHOLD,
   FIVE_HOUR_PENALTIES,
   FIVE_HOUR_ROTATION_CAP,
   LOW_USAGE_BONUS_CAP,
   LOW_USAGE_BONUS_FLOOR,
   LOW_USAGE_BONUS_GAIN,
   OPUS_BONUS_THRESHOLD,
   OPUS_HIGH_UTIL_PENALTY,
   OPUS_PACE_GATE,
   OPUS_PENALTY_THRESHOLD,
   SIMILAR_DRAIN_THRESHOLD,
)
from .models import Account, Candidate, UsageSnapshot

WINDOW_LENGTH_HOURS = 168.0
BOOTSTRAP_RESET_HOURS = 1.0  # treat accounts with no reset clock as immediately expiring
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

   # Default to 0 (available) when API returns null instead of 100 (exhausted)
   # Rationale: null typically means unused/untracked, not exhausted
   opus_util = float(opus_util_raw) if opus_util_raw is not None else 0.0
   overall_util = float(overall_util_raw) if overall_util_raw is not None else 0.0

   # Exhausted on both windows
   if opus_util >= 99.0 and overall_util >= 99.0:
      return None

   # Prefer overall window while it has headroom, fall back to opus otherwise
   if overall_util < 99.0:
      window = "overall"
      tier = 2
      utilization = overall_util
      hours_to_reset = usage.seven_day.hours_until_reset()
   else:
      window = "opus"
      tier = 1
      utilization = opus_util
      hours_to_reset = usage.seven_day_opus.hours_until_reset()

   # Core metrics
   no_reset_clock = not usage.seven_day.resets_at and not usage.seven_day_opus.resets_at
   if no_reset_clock:
      hours_to_reset = min(hours_to_reset, BOOTSTRAP_RESET_HOURS)

   headroom = max(99.0 - utilization, 0.0)
   effective_hours_left = max(hours_to_reset, 0.001)
   drain_rate = headroom / effective_hours_left if headroom > 0 else 0.0

   # Pace alignment only when opus is hot
   window_hours = WINDOW_LENGTH_HOURS
   elapsed_hours = max(window_hours - min(hours_to_reset, window_hours), 0.0)
   expected_utilization = (elapsed_hours / window_hours) * 100.0
   expected_utilization = max(0.0, min(expected_utilization, 100.0))
   pace_gap = expected_utilization - utilization
   pace_adjustment = 0.0
   if headroom > 0 and opus_util >= OPUS_PACE_GATE and opus_util < 99.0:
      pace_adjustment = (pace_gap / effective_hours_left) * PACE_GAIN
      if pace_gap < 0:
         pace_adjustment *= PACE_AHEAD_DAMPING
      pace_adjustment = max(min(pace_adjustment, MAX_PACE_ADJUSTMENT), -MAX_PACE_ADJUSTMENT)

   # Low usage bonus when opus is cool
   usage_bonus = 0.0
   if headroom > 0 and opus_util < OPUS_BONUS_THRESHOLD and utilization < LOW_USAGE_BONUS_CAP:
      clamped_util = max(utilization, LOW_USAGE_BONUS_FLOOR)
      normalized_gap = (LOW_USAGE_BONUS_CAP - clamped_util) / LOW_USAGE_BONUS_CAP
      usage_bonus = max(normalized_gap, 0.0) * LOW_USAGE_BONUS_GAIN

   # High opus penalty once we are near the ceiling
   high_util_penalty = OPUS_HIGH_UTIL_PENALTY if opus_util >= OPUS_PENALTY_THRESHOLD else 0.0

   priority_score = drain_rate + pace_adjustment + usage_bonus - high_util_penalty

   # 5-hour penalty
   five_hour_util_raw = usage.five_hour.utilization
   five_hour_util = float(five_hour_util_raw) if five_hour_util_raw is not None else 0.0

   five_hour_factor = 1.0
   for threshold, factor in FIVE_HOUR_PENALTIES:
      if five_hour_util >= threshold:
         five_hour_factor = factor
         break

   adjusted_drain = priority_score * five_hour_factor

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
      usage_bonus=usage_bonus,
      high_util_penalty=high_util_penalty,
      priority_score=priority_score,
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

   if candidate.priority_score >= high_drain_threshold and cache_age > 10:
      return True

   return False
