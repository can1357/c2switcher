"""Core domain models for c2switcher."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional


@dataclass
class Account:
   """
   Account entity with profile and credentials.

   Note: This dataclass is intentionally mutable (frozen=False) to allow
   in-place updates of credentials_json when tokens are refreshed. This
   prevents stale token issues during multi-step operations.
   """

   uuid: str
   index_num: int
   email: str
   credentials_json: str
   nickname: Optional[str] = None
   full_name: Optional[str] = None
   display_name: Optional[str] = None
   has_claude_max: bool = False
   has_claude_pro: bool = False
   org_uuid: Optional[str] = None
   org_name: Optional[str] = None
   org_type: Optional[str] = None
   billing_type: Optional[str] = None
   rate_limit_tier: Optional[str] = None
   created_at: Optional[str] = None
   updated_at: Optional[str] = None

   @classmethod
   def from_row(cls, row: sqlite3.Row) -> Account:
      """Convert SQLite row to Account model."""
      return cls(
         uuid=row["uuid"],
         index_num=row["index_num"],
         email=row["email"],
         credentials_json=row["credentials_json"],
         nickname=row["nickname"],
         full_name=row["full_name"],
         display_name=row["display_name"],
         has_claude_max=bool(row["has_claude_max"]),
         has_claude_pro=bool(row["has_claude_pro"]),
         org_uuid=row["org_uuid"],
         org_name=row["org_name"],
         org_type=row["org_type"],
         billing_type=row["billing_type"],
         rate_limit_tier=row["rate_limit_tier"],
         created_at=row["created_at"],
         updated_at=row["updated_at"],
      )

   @classmethod
   def from_dict(cls, data: Dict[str, Any]) -> Account:
      """Convert dict (legacy) to Account model."""
      return cls(
         uuid=data["uuid"],
         index_num=data["index_num"],
         email=data["email"],
         credentials_json=data["credentials_json"],
         nickname=data.get("nickname"),
         full_name=data.get("full_name"),
         display_name=data.get("display_name"),
         has_claude_max=bool(data.get("has_claude_max", False)),
         has_claude_pro=bool(data.get("has_claude_pro", False)),
         org_uuid=data.get("org_uuid"),
         org_name=data.get("org_name"),
         org_type=data.get("org_type"),
         billing_type=data.get("billing_type"),
         rate_limit_tier=data.get("rate_limit_tier"),
         created_at=data.get("created_at"),
         updated_at=data.get("updated_at"),
      )

   def get_credentials(self) -> Dict[str, Any]:
      """Parse credentials JSON."""
      return json.loads(self.credentials_json)

   def display_identifier(self) -> str:
      """Return nickname or display_name or email for UI."""
      return self.nickname or self.display_name or self.email

   def mask_email(self) -> str:
      """Return masked email for privacy."""
      if "@" not in self.email:
         return self.email[:3] + "***"
      local, domain = self.email.split("@", 1)
      return f"{local[:2]}***@{domain}"


@dataclass
class UsageWindow:
   """Usage metrics for a specific window (5h, 7d, 7d-opus)."""

   utilization: Optional[float] = None
   resets_at: Optional[str] = None

   def hours_until_reset(self) -> float:
      """Calculate hours until reset timestamp."""
      if not self.resets_at:
         return 168.0  # 7 days fallback
      try:
         reset_dt = datetime.fromisoformat(self.resets_at.replace("Z", "+00:00"))
         if reset_dt.tzinfo is None:
            reset_dt = reset_dt.replace(tzinfo=timezone.utc)
         hours = (reset_dt - datetime.now(timezone.utc)).total_seconds() / 3600.0
         if hours < 0:
            return 0.1
         return max(hours, 1.0 / 60.0)
      except Exception:
         return 168.0


@dataclass
class UsageSnapshot:
   """Complete usage snapshot for an account."""

   account_uuid: str
   five_hour: UsageWindow
   seven_day: UsageWindow
   seven_day_opus: UsageWindow
   queried_at: str
   cache_source: str = "cache"  # "cache" or "live"
   cache_age_seconds: float = 0.0

   @classmethod
   def from_api_response(cls, account_uuid: str, data: Dict[str, Any], source: str = "live") -> UsageSnapshot:
      """Build from API response dict."""
      five_hour_data = data.get("five_hour", {}) or {}
      seven_day_data = data.get("seven_day", {}) or {}
      seven_day_opus_data = data.get("seven_day_opus", {}) or {}

      return cls(
         account_uuid=account_uuid,
         five_hour=UsageWindow(
            utilization=five_hour_data.get("utilization"),
            resets_at=five_hour_data.get("resets_at"),
         ),
         seven_day=UsageWindow(
            utilization=seven_day_data.get("utilization"),
            resets_at=seven_day_data.get("resets_at"),
         ),
         seven_day_opus=UsageWindow(
            utilization=seven_day_opus_data.get("utilization"),
            resets_at=seven_day_opus_data.get("resets_at"),
         ),
         queried_at=data.get("_queried_at", datetime.now(timezone.utc).isoformat()),
         cache_source=data.get("_cache_source", source),
         cache_age_seconds=data.get("_cache_age_seconds", 0.0),
      )


@dataclass
class Session:
   """Session tracking entity."""

   session_id: str
   pid: int
   account_uuid: Optional[str] = None
   parent_pid: Optional[int] = None
   proc_start_time: Optional[float] = None
   exe: Optional[str] = None
   cmdline: Optional[str] = None
   cwd: Optional[str] = None
   created_at: Optional[str] = None
   last_checked_alive: Optional[str] = None
   ended_at: Optional[str] = None

   @classmethod
   def from_row(cls, row: sqlite3.Row) -> Session:
      """Convert SQLite row to Session model."""
      return cls(
         session_id=row["session_id"],
         pid=row["pid"],
         account_uuid=row["account_uuid"],
         parent_pid=row["parent_pid"],
         proc_start_time=row["proc_start_time"],
         exe=row["exe"],
         cmdline=row["cmdline"],
         cwd=row["cwd"],
         created_at=row["created_at"],
         last_checked_alive=row["last_checked_alive"],
         ended_at=row["ended_at"],
      )

   def is_active(self) -> bool:
      """Check if session is marked active."""
      return self.ended_at is None

   def duration_seconds(self) -> Optional[float]:
      """Calculate session duration if ended."""
      if not self.created_at or not self.ended_at:
         return None
      try:
         created = datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
         ended = datetime.fromisoformat(self.ended_at.replace("Z", "+00:00"))
         return (ended - created).total_seconds()
      except Exception:
         return None


@dataclass
class Candidate:
   """Load balancer candidate with scoring metadata."""

   account: Account
   usage: UsageSnapshot
   tier: int  # 1=opus, 2=overall
   window: str  # "opus" or "overall"
   utilization: float
   headroom: float
   hours_to_reset: float
   drain_rate: float
   expected_utilization: float
   pace_gap: float
   pace_adjustment: float
   fresh_bonus: float
   priority_drain: float
   five_hour_utilization: float
   five_hour_factor: float
   adjusted_drain: float
   expected_burst: float
   burst_blocked: bool
   active_sessions: int
   recent_sessions: int
   refreshed: bool = False

   @property
   def rank(self) -> tuple:
      """Multi-dimensional sort key for selection."""
      return (
         self.adjusted_drain,
         self.utilization,
         -self.hours_to_reset,
         -self.five_hour_utilization,
         -self.active_sessions,
         -self.recent_sessions,
      )


@dataclass
class SelectionRequest:
   """Input parameters for account selection."""

   session_id: Optional[str] = None
   allow_burst_blocked: bool = False
   allow_high_five_hour: bool = False


@dataclass
class SelectionDecision:
   """Result of load balancer selection with full diagnostics."""

   account: Account
   tier: int
   window: str
   opus_usage: Optional[float]
   overall_usage: Optional[float]
   headroom: float
   hours_to_reset: float
   drain_rate: float
   expected_utilization: float
   pace_gap: float
   pace_adjustment: float
   priority_drain: float
   fresh_bonus: float
   adjusted_drain: float
   five_hour_factor: float
   five_hour_utilization: float
   expected_burst: float
   burst_blocked: bool
   active_sessions: int
   recent_sessions: int
   cache_source: str
   cache_age_seconds: Optional[float]
   refreshed: bool
   reused: bool
   rank: tuple = field(default_factory=tuple)

   @classmethod
   def from_candidate(cls, candidate: Candidate, reused: bool = False) -> SelectionDecision:
      """Build decision from selected candidate."""
      return cls(
         account=candidate.account,
         tier=candidate.tier,
         window=candidate.window,
         opus_usage=candidate.usage.seven_day_opus.utilization,
         overall_usage=candidate.usage.seven_day.utilization,
         headroom=candidate.headroom,
         hours_to_reset=candidate.hours_to_reset,
         drain_rate=candidate.drain_rate,
         expected_utilization=candidate.expected_utilization,
         pace_gap=candidate.pace_gap,
         pace_adjustment=candidate.pace_adjustment,
         priority_drain=candidate.priority_drain,
         fresh_bonus=candidate.fresh_bonus,
         adjusted_drain=candidate.adjusted_drain,
         five_hour_factor=candidate.five_hour_factor,
         five_hour_utilization=candidate.five_hour_utilization,
         expected_burst=candidate.expected_burst,
         burst_blocked=candidate.burst_blocked,
         active_sessions=candidate.active_sessions,
         recent_sessions=candidate.recent_sessions,
         cache_source=candidate.usage.cache_source,
         cache_age_seconds=candidate.usage.cache_age_seconds,
         refreshed=candidate.refreshed,
         reused=reused,
         rank=candidate.rank,
      )
