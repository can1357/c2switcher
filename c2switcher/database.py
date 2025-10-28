"""SQLite persistence layer."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .constants import C2SWITCHER_DIR, DB_PATH, DEFAULT_BURST_BUFFER


class Database:
    """SQLite database manager for account, usage, and session data."""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.conn: Optional[sqlite3.Connection] = None
        self.init_db()

    def init_db(self):
        """Initialize database schema."""
        C2SWITCHER_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(C2SWITCHER_DIR, 0o700)
        except OSError:
            pass

        self.conn = sqlite3.connect(str(self.db_path), timeout=5)
        self.conn.row_factory = sqlite3.Row

        try:
            os.chmod(self.db_path, 0o600)
        except (FileNotFoundError, OSError):
            pass

        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")

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

        self.conn.commit()

    def get_next_index(self) -> int:
        cursor = self.conn.cursor()
        cursor.execute("SELECT MAX(index_num) FROM accounts")
        result = cursor.fetchone()[0]
        return 0 if result is None else result + 1

    def add_account(self, profile: Dict, credentials: Dict, nickname: Optional[str] = None) -> int:
        account = profile.get("account", {})
        org = profile.get("organization", {})
        uuid = account.get("uuid")

        if not uuid:
            raise ValueError("Invalid profile data: missing account UUID")

        with self.conn:
            cursor = self.conn.cursor()
            cursor.execute("SELECT id, index_num FROM accounts WHERE uuid = ?", (uuid,))
            existing = cursor.fetchone()

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
                        account.get("email"),
                        account.get("full_name"),
                        account.get("display_name"),
                        account.get("has_claude_max", False),
                        account.get("has_claude_pro", False),
                        org.get("uuid"),
                        org.get("name"),
                        org.get("organization_type"),
                        org.get("billing_type"),
                        org.get("rate_limit_tier"),
                        json.dumps(credentials),
                        uuid,
                    ),
                )
                return existing[1]

            index_num = self.get_next_index()
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
                    account.get("email"),
                    account.get("full_name"),
                    account.get("display_name"),
                    account.get("has_claude_max", False),
                    account.get("has_claude_pro", False),
                    org.get("uuid"),
                    org.get("name"),
                    org.get("organization_type"),
                    org.get("billing_type"),
                    org.get("rate_limit_tier"),
                    json.dumps(credentials),
                ),
            )
            return index_num

    def add_usage(self, account_uuid: str, usage_data: Dict):
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

    def get_recent_usage(self, account_uuid: str, max_age_seconds: int = 30) -> Optional[Tuple[Dict, str]]:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT raw_response, queried_at
            FROM usage_history
            WHERE account_uuid = ?
            AND datetime(queried_at) > datetime('now', ? || ' seconds')
            ORDER BY queried_at DESC LIMIT 1
            """,
            (account_uuid, f"-{max_age_seconds}"),
        )
        row = cursor.fetchone()
        if row:
            return json.loads(row[0]), row[1]
        return None

    def get_usage_delta_percentile(self, account_uuid: str, percentile: float = 95.0, limit: int = 25) -> float:
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

    def get_all_accounts(self) -> List[sqlite3.Row]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM accounts ORDER BY index_num")
        return cursor.fetchall()

    def get_account_by_identifier(self, identifier: str) -> Optional[sqlite3.Row]:
        cursor = self.conn.cursor()

        if identifier.isdigit():
            cursor.execute("SELECT * FROM accounts WHERE index_num = ?", (int(identifier),))
            row = cursor.fetchone()
            if row:
                return row

        cursor.execute(
            """
            SELECT * FROM accounts
            WHERE nickname = ? OR email = ? OR uuid = ?
            """,
            (identifier, identifier, identifier),
        )
        return cursor.fetchone()

    def get_latest_usage_for_all_accounts(self) -> Dict[str, Dict]:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT account_uuid, raw_response
            FROM usage_history
            WHERE (
                account_uuid,
                queried_at
            ) IN (
                SELECT account_uuid, MAX(queried_at)
                FROM usage_history
                GROUP BY account_uuid
            )
            """
        )
        result: Dict[str, Dict] = {}
        for account_uuid, raw in cursor.fetchall():
            result[account_uuid] = json.loads(raw)
        return result

    def count_active_sessions(self, account_uuid: str) -> int:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT COUNT(*) FROM sessions
            WHERE account_uuid = ? AND ended_at IS NULL
            """,
            (account_uuid,),
        )
        return cursor.fetchone()[0]

    def create_session(
        self,
        session_id: str,
        pid: int,
        parent_pid: Optional[int],
        proc_start_time: float,
        exe: str,
        cmdline: str,
        cwd: str,
    ):
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

    def get_active_sessions(self) -> List[sqlite3.Row]:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT * FROM sessions
            WHERE ended_at IS NULL
            ORDER BY created_at DESC
            """
        )
        return cursor.fetchall()

    def get_session_account(self, session_id: str) -> Optional[sqlite3.Row]:
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
        return cursor.fetchone()

    def count_recent_sessions(self, account_uuid: str, minutes: int = 5) -> int:
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

    def assign_session_to_account(self, session_id: str, account_uuid: str):
        cursor = self.conn.cursor()
        cursor.execute(
            """
            UPDATE sessions
            SET account_uuid = ?
            WHERE session_id = ?
            """,
            (account_uuid, session_id),
        )
        self.conn.commit()

    def mark_session_ended(self, session_id: str):
        cursor = self.conn.cursor()
        cursor.execute(
            """
            UPDATE sessions
            SET ended_at = CURRENT_TIMESTAMP
            WHERE session_id = ?
            """,
            (session_id,),
        )
        self.conn.commit()

    def update_session_last_checked(self, session_id: str):
        cursor = self.conn.cursor()
        cursor.execute(
            """
            UPDATE sessions
            SET last_checked_alive = CURRENT_TIMESTAMP
            WHERE session_id = ?
            """,
            (session_id,),
        )
        self.conn.commit()

    def get_session_history(self, min_duration_seconds: int = 5, limit: int = 50) -> List[sqlite3.Row]:
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
        return cursor.fetchall()

    def get_usage_before(self, account_uuid: str, timestamp: str) -> Optional[Dict]:
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
        if self.conn:
            self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
