"""SQLite repository wrapper returning domain models."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..constants import C2SWITCHER_DIR, DB_PATH, DEFAULT_BURST_BUFFER
from ..core.models import Account, Session, UsageSnapshot


class Store:
   """
   Repository layer for account, usage, and session persistence.

   Encapsulates SQLite connection management and returns typed models
   instead of raw Row objects.
   """

   def __init__(self, db_path: Path = DB_PATH):
      self.db_path = db_path
      self.conn: Optional[sqlite3.Connection] = None
      self._init_connection()

   def _init_connection(self):
      """Initialize database connection with required PRAGMAs."""
      C2SWITCHER_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
      try:
         import os
         os.chmod(C2SWITCHER_DIR, 0o700)
      except OSError:
         pass

      self.conn = sqlite3.connect(str(self.db_path), timeout=5)
      self.conn.row_factory = sqlite3.Row

      try:
         import os
         os.chmod(self.db_path, 0o600)
      except (FileNotFoundError, OSError):
         pass

      self.conn.execute("PRAGMA foreign_keys = ON")
      self.conn.execute("PRAGMA journal_mode=WAL")
      self.conn.execute("PRAGMA busy_timeout=5000")

      self._create_schema()

   def _create_schema(self):
      """Ensure schema exists (unchanged from Database class)."""
      cursor = self.conn.cursor()

      cursor.execute(
         """
         CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid TEXT UNIQUE NOT NULL,
            index_num INTEGER UNIQUE NOT NULL,
            nickname TEXT,
            email TEXT NOT NULL,
            full_name TEXT,
            display_name TEXT,
            has_claude_max BOOLEAN,
            has_claude_pro BOOLEAN,
            org_uuid TEXT,
            org_name TEXT,
            org_type TEXT,
            billing_type TEXT,
            rate_limit_tier TEXT,
            credentials_json TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
         )
         """
      )

      cursor.execute(
         """
         CREATE TABLE IF NOT EXISTS usage_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_uuid TEXT NOT NULL,
            queried_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            five_hour_utilization INTEGER,
            five_hour_resets_at TEXT,
            seven_day_utilization INTEGER,
            seven_day_resets_at TEXT,
            seven_day_opus_utilization INTEGER,
            seven_day_opus_resets_at TEXT,
            raw_response TEXT NOT NULL,
            FOREIGN KEY (account_uuid) REFERENCES accounts(uuid)
         )
         """
      )

      cursor.execute(
         """
         CREATE INDEX IF NOT EXISTS idx_usage_account_queried
         ON usage_history(account_uuid, queried_at DESC)
         """
      )

      cursor.execute(
         """
         CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            account_uuid TEXT,
            pid INTEGER NOT NULL,
            parent_pid INTEGER,
            proc_start_time REAL,
            exe TEXT,
            cmdline TEXT,
            cwd TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_checked_alive TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            ended_at TIMESTAMP,
            FOREIGN KEY (account_uuid) REFERENCES accounts(uuid) ON DELETE SET NULL
         )
         """
      )

      cursor.execute(
         """
         CREATE INDEX IF NOT EXISTS idx_sessions_active_created
         ON sessions(created_at DESC)
         WHERE ended_at IS NULL
         """
      )

      cursor.execute(
         """
         CREATE INDEX IF NOT EXISTS idx_sessions_account
         ON sessions(account_uuid)
         """
      )

      cursor.execute(
         """
         CREATE TABLE IF NOT EXISTS round_robin_state (
            window TEXT PRIMARY KEY,
            last_account_uuid TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
         )
         """
      )

      self.conn.commit()

   # Account operations
   def list_accounts(self) -> List[Account]:
      """Retrieve all accounts ordered by index."""
      cursor = self.conn.cursor()
      cursor.execute("SELECT * FROM accounts ORDER BY index_num")
      return [Account.from_row(row) for row in cursor.fetchall()]

   def get_account_by_uuid(self, uuid: str) -> Optional[Account]:
      """Retrieve account by UUID."""
      cursor = self.conn.cursor()
      cursor.execute("SELECT * FROM accounts WHERE uuid = ?", (uuid,))
      row = cursor.fetchone()
      return Account.from_row(row) if row else None

   def get_account_by_identifier(self, identifier: str) -> Optional[Account]:
      """Retrieve account by index, nickname, email, or UUID."""
      cursor = self.conn.cursor()

      if identifier.isdigit():
         cursor.execute("SELECT * FROM accounts WHERE index_num = ?", (int(identifier),))
         row = cursor.fetchone()
         if row:
            return Account.from_row(row)

      cursor.execute(
         "SELECT * FROM accounts WHERE nickname = ? OR email = ? OR uuid = ?",
         (identifier, identifier, identifier),
      )
      row = cursor.fetchone()
      return Account.from_row(row) if row else None

   def save_account(
      self, profile: Dict, credentials: Dict, nickname: Optional[str] = None
   ) -> Tuple[Account, bool]:
      """
      Save or update account from profile data.

      Returns (Account, is_new) tuple.
      """
      account_data = profile.get("account", {})
      org = profile.get("organization", {})
      uuid = account_data.get("uuid")

      if not uuid:
         raise ValueError("Invalid profile data: missing account UUID")

      with self.conn:
         cursor = self.conn.cursor()
         cursor.execute("SELECT id, index_num FROM accounts WHERE uuid = ?", (uuid,))
         existing = cursor.fetchone()

         credentials_json = json.dumps(credentials)

         if existing:
            cursor.execute(
               """
               UPDATE accounts SET
                  nickname = COALESCE(?, nickname),
                  email = ?,
                  full_name = ?,
                  display_name = ?,
                  has_claude_max = ?,
                  has_claude_pro = ?,
                  org_uuid = ?,
                  org_name = ?,
                  org_type = ?,
                  billing_type = ?,
                  rate_limit_tier = ?,
                  credentials_json = ?,
                  updated_at = CURRENT_TIMESTAMP
               WHERE uuid = ?
               """,
               (
                  nickname,
                  account_data.get("email"),
                  account_data.get("full_name"),
                  account_data.get("display_name"),
                  account_data.get("has_claude_max", False),
                  account_data.get("has_claude_pro", False),
                  org.get("uuid"),
                  org.get("name"),
                  org.get("organization_type"),
                  org.get("billing_type"),
                  org.get("rate_limit_tier"),
                  credentials_json,
                  uuid,
               ),
            )
            account = self.get_account_by_uuid(uuid)
            return account, False

         # New account
         cursor.execute("SELECT MAX(index_num) FROM accounts")
         max_index = cursor.fetchone()[0]
         index_num = 0 if max_index is None else max_index + 1

         cursor.execute(
            """
            INSERT INTO accounts (
               uuid, index_num, nickname, email, full_name, display_name,
               has_claude_max, has_claude_pro, org_uuid, org_name, org_type,
               billing_type, rate_limit_tier, credentials_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
               uuid,
               index_num,
               nickname,
               account_data.get("email"),
               account_data.get("full_name"),
               account_data.get("display_name"),
               account_data.get("has_claude_max", False),
               account_data.get("has_claude_pro", False),
               org.get("uuid"),
               org.get("name"),
               org.get("organization_type"),
               org.get("billing_type"),
               org.get("rate_limit_tier"),
               credentials_json,
            ),
         )

         account = self.get_account_by_uuid(uuid)
         return account, True

   def update_credentials(self, account_uuid: str, credentials: Dict):
      """Update account credentials JSON."""
      with self.conn:
         cursor = self.conn.cursor()
         cursor.execute(
            "UPDATE accounts SET credentials_json = ?, updated_at = CURRENT_TIMESTAMP WHERE uuid = ?",
            (json.dumps(credentials), account_uuid),
         )

   # Usage operations
   def save_usage(self, account_uuid: str, usage_data: Dict):
      """Persist usage snapshot."""
      cursor = self.conn.cursor()
      five_hour = usage_data.get("five_hour", {}) or {}
      seven_day = usage_data.get("seven_day", {}) or {}
      seven_day_opus = usage_data.get("seven_day_opus", {}) or {}

      cursor.execute(
         """
         INSERT INTO usage_history (
            account_uuid, five_hour_utilization, five_hour_resets_at,
            seven_day_utilization, seven_day_resets_at,
            seven_day_opus_utilization, seven_day_opus_resets_at,
            raw_response
         ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
         """,
         (
            account_uuid,
            five_hour.get("utilization"),
            five_hour.get("resets_at"),
            seven_day.get("utilization"),
            seven_day.get("resets_at"),
            seven_day_opus.get("utilization"),
            seven_day_opus.get("resets_at"),
            json.dumps(usage_data),
         ),
      )
      self.conn.commit()

   def get_recent_usage(self, account_uuid: str, max_age_seconds: int = 300) -> Optional[UsageSnapshot]:
      """Retrieve cached usage within age threshold."""
      cursor = self.conn.cursor()
      cutoff_time = time.time() - max_age_seconds
      cursor.execute(
         """
         SELECT raw_response, queried_at
         FROM usage_history
         WHERE account_uuid = ?
         AND strftime('%s', queried_at) > ?
         ORDER BY queried_at DESC LIMIT 1
         """,
         (account_uuid, str(int(cutoff_time))),
      )
      row = cursor.fetchone()
      if not row:
         return None

      usage_data = json.loads(row[0])
      queried_at = row[1]

      # Compute cache age
      from datetime import datetime, timezone
      try:
         cache_dt = datetime.fromisoformat(queried_at.replace("Z", "+00:00"))
         if cache_dt.tzinfo is None:
            cache_dt = cache_dt.replace(tzinfo=timezone.utc)
         cache_age = max((datetime.now(timezone.utc) - cache_dt).total_seconds(), 0)
      except Exception:
         cache_age = 0.0

      usage_data["_cache_source"] = "cache"
      usage_data["_cache_age_seconds"] = cache_age
      usage_data["_queried_at"] = queried_at

      return UsageSnapshot.from_api_response(account_uuid, usage_data, source="cache")

   def get_burst_percentile(self, account_uuid: str, percentile: float = 95.0, limit: int = 25) -> float:
      """Calculate usage delta percentile for burst prediction."""
      cursor = self.conn.cursor()
      cursor.execute(
         """
         SELECT seven_day_opus_utilization, seven_day_utilization
         FROM usage_history
         WHERE account_uuid = ?
         ORDER BY queried_at DESC
         LIMIT ?
         """,
         (account_uuid, limit),
      )
      rows = cursor.fetchall()
      if len(rows) < 2:
         return DEFAULT_BURST_BUFFER

      deltas: List[float] = []
      prev_opus: Optional[float] = None
      prev_overall: Optional[float] = None

      for row in rows:
         opus_util, overall_util = row
         if prev_opus is not None and opus_util is not None:
            deltas.append(abs(prev_opus - opus_util))
         if prev_overall is not None and overall_util is not None:
            deltas.append(abs(prev_overall - overall_util))
         prev_opus = opus_util if opus_util is not None else prev_opus
         prev_overall = overall_util if overall_util is not None else prev_overall

      deltas = [d for d in deltas if d is not None]
      if not deltas:
         return DEFAULT_BURST_BUFFER

      deltas.sort()
      pct = max(0.0, min(100.0, percentile))
      pos = pct / 100.0 * (len(deltas) - 1)
      lower = int(pos)
      upper = min(lower + 1, len(deltas) - 1)
      if lower == upper:
         return float(deltas[lower])
      frac = pos - lower
      return float(deltas[lower] + (deltas[upper] - deltas[lower]) * frac)

   # Session operations
   def create_session(
      self,
      session_id: str,
      pid: int,
      parent_pid: Optional[int],
      proc_start_time: float,
      exe: str,
      cmdline: str,
      cwd: str,
   ) -> Session:
      """Register new session."""
      cursor = self.conn.cursor()
      cursor.execute(
         """
         INSERT INTO sessions (
            session_id, pid, parent_pid, proc_start_time,
            exe, cmdline, cwd
         ) VALUES (?, ?, ?, ?, ?, ?, ?)
         """,
         (session_id, pid, parent_pid, proc_start_time, exe, cmdline, cwd),
      )
      self.conn.commit()

      cursor.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
      return Session.from_row(cursor.fetchone())

   def assign_session_to_account(self, session_id: str, account_uuid: str):
      """Bind session to account."""
      with self.conn:
         cursor = self.conn.cursor()
         cursor.execute(
            "UPDATE sessions SET account_uuid = ? WHERE session_id = ?",
            (account_uuid, session_id),
         )

   def get_session_account(self, session_id: str) -> Optional[Tuple[Session, Account]]:
      """Retrieve active session with its assigned account."""
      cursor = self.conn.cursor()
      cursor.execute(
         """
         SELECT sessions.*, accounts.*
         FROM sessions
         JOIN accounts ON sessions.account_uuid = accounts.uuid
         WHERE sessions.session_id = ? AND sessions.ended_at IS NULL
         """,
         (session_id,),
      )
      row = cursor.fetchone()
      if not row:
         return None

      # Split row into session and account fields
      session_cols = [
         "session_id",
         "account_uuid",
         "pid",
         "parent_pid",
         "proc_start_time",
         "exe",
         "cmdline",
         "cwd",
         "created_at",
         "last_checked_alive",
         "ended_at",
      ]
      session_dict = {k: row[k] for k in session_cols}
      account_dict = {k: row[k] for k in row.keys() if k not in session_cols}

      session = Session(**session_dict)
      account = Account.from_dict(account_dict)

      return session, account

   def list_active_sessions(self) -> List[Session]:
      """Retrieve all active sessions."""
      cursor = self.conn.cursor()
      cursor.execute("SELECT * FROM sessions WHERE ended_at IS NULL ORDER BY created_at DESC")
      return [Session.from_row(row) for row in cursor.fetchall()]

   def count_active_sessions(self, account_uuid: str) -> int:
      """Count active sessions for account."""
      cursor = self.conn.cursor()
      cursor.execute(
         "SELECT COUNT(*) FROM sessions WHERE account_uuid = ? AND ended_at IS NULL",
         (account_uuid,),
      )
      return cursor.fetchone()[0]

   def get_active_session_counts(self) -> Dict[str, int]:
      """Fetch active session counts for all accounts."""
      cursor = self.conn.cursor()
      cursor.execute(
         """
         SELECT account_uuid, COUNT(*) as count
         FROM sessions
         WHERE ended_at IS NULL AND account_uuid IS NOT NULL
         GROUP BY account_uuid
         """
      )
      return {row[0]: row[1] for row in cursor.fetchall()}

   def count_recent_sessions(self, account_uuid: str, minutes: int = 5) -> int:
      """Count sessions created within N minutes."""
      cursor = self.conn.cursor()
      cursor.execute(
         """
         SELECT COUNT(*) FROM sessions
         WHERE account_uuid = ?
           AND datetime(created_at) >= datetime('now', '-' || ? || ' minutes')
         """,
         (account_uuid, minutes),
      )
      return cursor.fetchone()[0]

   def get_recent_session_counts(self, minutes: int = 5) -> Dict[str, int]:
      """Get recent session counts for all accounts."""
      cursor = self.conn.cursor()
      cursor.execute(
         """
         SELECT account_uuid, COUNT(*) as count
         FROM sessions
         WHERE account_uuid IS NOT NULL
           AND datetime(created_at) >= datetime('now', '-' || ? || ' minutes')
         GROUP BY account_uuid
         """,
         (minutes,),
      )
      return {row[0]: row[1] for row in cursor.fetchall()}

   def mark_session_ended(self, session_id: str):
      """Mark session as ended."""
      cursor = self.conn.cursor()
      cursor.execute(
         "UPDATE sessions SET ended_at = CURRENT_TIMESTAMP WHERE session_id = ?",
         (session_id,),
      )
      self.conn.commit()

   def update_session_last_checked(self, session_id: str):
      """Update last liveness check timestamp."""
      cursor = self.conn.cursor()
      cursor.execute(
         "UPDATE sessions SET last_checked_alive = CURRENT_TIMESTAMP WHERE session_id = ?",
         (session_id,),
      )
      self.conn.commit()

   # Round-robin state operations
   def get_round_robin_last(self, window: str) -> Optional[str]:
      """Get last selected account UUID for given window."""
      cursor = self.conn.cursor()
      cursor.execute("SELECT last_account_uuid FROM round_robin_state WHERE window = ?", (window,))
      row = cursor.fetchone()
      return row[0] if row else None

   def set_round_robin_last(self, window: str, account_uuid: str):
      """Set last selected account UUID for given window."""
      cursor = self.conn.cursor()
      cursor.execute(
         """
         INSERT INTO round_robin_state (window, last_account_uuid, updated_at)
         VALUES (?, ?, CURRENT_TIMESTAMP)
         ON CONFLICT(window) DO UPDATE SET
            last_account_uuid = excluded.last_account_uuid,
            updated_at = CURRENT_TIMESTAMP
         """,
         (window, account_uuid),
      )
      self.conn.commit()

   def migrate_legacy_round_robin_state(self):
      """
      One-time migration from legacy load_balancer_state.json to SQLite.

      Returns number of entries migrated.
      """
      from ..constants import LB_STATE_PATH

      if not LB_STATE_PATH.exists():
         return 0

      try:
         import json
         with LB_STATE_PATH.open("r", encoding="utf-8") as f:
            legacy_state = json.load(f)

         round_robin = legacy_state.get("round_robin", {})
         if not isinstance(round_robin, dict):
            return 0

         migrated = 0
         for window, account_uuid in round_robin.items():
            if account_uuid:
               self.set_round_robin_last(window, account_uuid)
               migrated += 1

         # Rename legacy file to mark as migrated
         LB_STATE_PATH.rename(LB_STATE_PATH.with_suffix(".json.migrated"))

         return migrated

      except Exception:
         # Migration failure is non-fatal
         return 0

   # Session history and usage queries
   def get_session_history(self, min_duration_seconds: int = 5, limit: int = 50) -> List[Session]:
      """Get historical sessions with minimum duration."""
      cursor = self.conn.cursor()
      cursor.execute(
         """
         SELECT *,
                (julianday(ended_at) - julianday(created_at)) * 86400 as duration_seconds
         FROM sessions
         WHERE ended_at IS NOT NULL
           AND (julianday(ended_at) - julianday(created_at)) * 86400 >= ?
         ORDER BY ended_at DESC
         LIMIT ?
         """,
         (min_duration_seconds, limit),
      )
      return [Session.from_row(row) for row in cursor.fetchall()]

   def get_usage_before(self, account_uuid: str, timestamp: str) -> Optional[Dict]:
      """Get latest usage snapshot before given timestamp."""
      cursor = self.conn.cursor()
      cursor.execute(
         """
         SELECT raw_response, queried_at
         FROM usage_history
         WHERE account_uuid = ? AND queried_at <= ?
         ORDER BY queried_at DESC
         LIMIT 1
         """,
         (account_uuid, timestamp),
      )
      row = cursor.fetchone()
      if row:
         return {"data": json.loads(row[0]), "queried_at": row[1]}
      return None

   def get_usage_after(self, account_uuid: str, timestamp: str) -> Optional[Dict]:
      """Get earliest usage snapshot after given timestamp."""
      cursor = self.conn.cursor()
      cursor.execute(
         """
         SELECT raw_response, queried_at
         FROM usage_history
         WHERE account_uuid = ? AND queried_at >= ?
         ORDER BY queried_at ASC
         LIMIT 1
         """,
         (account_uuid, timestamp),
      )
      row = cursor.fetchone()
      if row:
         return {"data": json.loads(row[0]), "queried_at": row[1]}
      return None

   def close(self):
      """Close database connection."""
      if self.conn:
         self.conn.close()

   def __enter__(self):
      return self

   def __exit__(self, exc_type, exc_val, exc_tb):
      self.close()
      return False
