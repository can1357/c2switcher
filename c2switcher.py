#!/usr/bin/env python3
"""
Claude Code Account Switcher - Manage multiple Claude Code accounts
"""

import atexit
import contextlib
import copy
import json
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List
import requests
import psutil
import click
from filelock import FileLock as FileLocker, Timeout as FileLockTimeout
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

console = Console(stderr=True)

# Paths
C2SWITCHER_DIR = Path.home() / ".c2switcher"
DB_PATH = C2SWITCHER_DIR / "store.db"
LOCK_PATH = C2SWITCHER_DIR / ".lock"
HEADERS_PATH = C2SWITCHER_DIR / "headers.json"
CLAUDE_DIR = Path.home() / ".claude"
CREDENTIALS_PATH = CLAUDE_DIR / ".credentials.json"

# Global lock state
_lock_acquired = None  # Stores the FileLock instance if acquired


class FileLock:
    """
    Cross-platform file-based locking mechanism to prevent concurrent write operations.
    Uses filelock library which works on both Windows (msvcrt) and POSIX (fcntl).
    """

    def __init__(self, lock_path: Path = LOCK_PATH):
        self.lock_path = lock_path
        self.pid_path = lock_path.with_suffix('.pid')
        self.lock = FileLocker(str(lock_path), timeout=-1)  # Non-blocking by default
        self.acquired = False

    def acquire(self, timeout: int = 30, max_retries: int = 300):
        """Acquire exclusive lock, waiting up to timeout seconds with max retries"""
        start_time = time.time()
        shown_waiting_msg = False
        retries = 0

        # Ensure lock directory exists with secure permissions
        self.lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self.lock_path.parent, 0o700)
        except OSError:
            pass

        while retries < max_retries:
            try:
                # Try to acquire lock (non-blocking)
                self.lock.acquire(timeout=0.001)
                self.acquired = True

                # Write PID to separate file for debugging (after lock is acquired)
                try:
                    fd = os.open(self.pid_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, 'w') as f:
                        f.write(f"{os.getpid()}\n")
                        f.flush()
                        os.fsync(f.fileno())  # Ensure other processes see PID immediately
                except OSError:
                    pass  # Non-critical

                # Successfully acquired
                if shown_waiting_msg:
                    console.print("[green]✓ Lock acquired[/green]")
                return

            except FileLockTimeout:
                # Lock is held by another process
                retries += 1
                elapsed = time.time() - start_time

                if elapsed >= timeout:
                    # Timeout reached - try to show which PID holds the lock
                    pid_info = self._read_pid()
                    if pid_info:
                        console.print(f"[red]Error: Timeout waiting for c2switcher operation (PID: {pid_info}) to complete[/red]")
                    else:
                        console.print("[red]Error: Timeout waiting for c2switcher operation to complete[/red]")
                    sys.exit(1)

                # Show waiting message once
                if not shown_waiting_msg:
                    pid_info = self._read_pid()
                    if pid_info:
                        console.print(f"[yellow]Waiting for another c2switcher operation to complete (PID: {pid_info})...[/yellow]")
                    else:
                        console.print("[yellow]Waiting for another c2switcher operation to complete...[/yellow]")
                    shown_waiting_msg = True

                # Wait a bit before retrying
                time.sleep(0.1)

            except Exception as e:
                console.print(f"[red]Error acquiring lock: {e}[/red]")
                sys.exit(1)

        # Max retries exceeded
        console.print(f"[red]Error: Maximum retries ({max_retries}) exceeded waiting for lock[/red]")
        sys.exit(1)

    def _read_pid(self) -> Optional[str]:
        """Read PID from lock file for debugging"""
        try:
            if self.pid_path.exists():
                with open(self.pid_path, 'r') as f:
                    return f.read().strip()
        except:
            pass
        return None

    def release(self):
        """Release the lock"""
        if self.acquired:
            try:
                self.lock.release()
                self.acquired = False

                # Clean up PID file
                with contextlib.suppress(FileNotFoundError, OSError):
                    self.pid_path.unlink()
            except Exception:
                pass


def acquire_lock():
    """
    Acquire an exclusive file lock to prevent concurrent write operations.
    Idempotent - can be called multiple times, will only acquire once.

    Read operations don't need locks - SQLite with WAL mode handles concurrent reads.
    Lock is needed for: writing .credentials.json, complex read-modify-write operations.
    """
    global _lock_acquired

    # If already acquired, do nothing (idempotent)
    if _lock_acquired is not None:
        return

    # Create and acquire lock
    lock = FileLock()
    lock.acquire()
    _lock_acquired = lock

    # Register cleanup on exit
    atexit.register(_release_lock)


def _release_lock():
    """Internal function to release the global lock on exit"""
    global _lock_acquired
    if _lock_acquired is not None:
        _lock_acquired.release()
        _lock_acquired = None


def mask_email(email: str) -> str:
    """Mask email keeping first 2 and last 2 letters before @"""
    if '@' not in email:
        return email

    local, domain = email.split('@', 1)

    if len(local) <= 4:
        # If too short, just mask the middle
        return f"{local[0]}***{local[-1]}@{domain}" if len(local) > 1 else f"{local}@{domain}"

    # Keep first 2 and last 2, mask the rest
    masked_local = f"{local[:2]}{'*' * (len(local) - 4)}{local[-2:]}"
    return f"{masked_local}@{domain}"


def format_time_until_reset(resets_at: Optional[str], opus_usage: Optional[int] = None, overall_usage: Optional[int] = None) -> str:
    """
    Format time remaining until reset timestamp with usage rate percentage.

    Args:
        resets_at: ISO format timestamp string (e.g., "2025-10-30T12:00:00Z")
        opus_usage: Current opus usage percentage (0-100)
        overall_usage: Current overall usage percentage (0-100)

    Returns:
        Formatted time string with usage rate (e.g., "2d 5h (140%)", "6h 30m (85%)")
        Usage rate = max(opus, overall) / (time_elapsed / 7_days) * 100
    """
    if not resets_at:
        return "[dim]--[/dim]"

    try:
        # Parse ISO timestamp
        reset_dt = datetime.fromisoformat(resets_at.replace('Z', '+00:00'))

        # Ensure it's treated as UTC if naive
        if reset_dt.tzinfo is None:
            reset_dt = reset_dt.replace(tzinfo=timezone.utc)

        # Calculate time remaining
        now = datetime.now(timezone.utc)
        time_remaining = reset_dt - now

        # If already expired, show as expired
        if time_remaining.total_seconds() <= 0:
            return "[dim]expired[/dim]"

        total_seconds = int(time_remaining.total_seconds())
        days = total_seconds // 86400
        hours = (total_seconds % 86400) // 3600
        minutes = (total_seconds % 3600) // 60

        # Format time based on magnitude (compact format)
        if days > 0:
            time_str = f"{days}d{hours}h"
        elif hours > 0:
            time_str = f"{hours}h{minutes}m"
        else:
            time_str = f"{minutes}m"

        # Calculate usage rate if usage data provided
        rate_str = ""
        if opus_usage is not None and overall_usage is not None:
            # 7 days in seconds
            seven_days_seconds = 7 * 86400
            # Time elapsed since start of period
            time_elapsed_seconds = seven_days_seconds - time_remaining.total_seconds()
            # Expected usage based on linear progression
            expected_usage = (time_elapsed_seconds / seven_days_seconds) * 100

            if expected_usage > 0:
                # Actual usage is max of opus and overall
                actual_usage = max(opus_usage, overall_usage)
                # Usage rate as percentage
                usage_rate = (actual_usage / expected_usage) * 100

                # Color code the rate
                if usage_rate >= 120:
                    rate_str = f" [red]({usage_rate:.0f}%)[/red]"
                elif usage_rate >= 100:
                    rate_str = f" [yellow]({usage_rate:.0f}%)[/yellow]"
                else:
                    rate_str = f" [green]({usage_rate:.0f}%)[/green]"

        return time_str + rate_str

    except Exception:
        return "[dim]--[/dim]"


def atomic_write_json(path: Path, data: Dict, preserve_permissions: bool = True):
    """
    Atomically write JSON data to a file.

    Uses a temporary file and os.replace() to ensure atomic writes.
    Optionally preserves existing file permissions.

    Args:
        path: Target file path
        data: Dictionary to write as JSON
        preserve_permissions: If True, copy permissions from existing file
    """
    # Ensure parent directory exists with secure permissions
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass  # Best effort

    # Query existing permissions if requested
    mode = 0o600  # Default: owner read/write only
    if preserve_permissions and path.exists():
        try:
            stat_info = path.stat()
            mode = stat_info.st_mode & 0o777  # Extract permission bits
        except OSError:
            pass  # Fall back to default

    # Write to temporary file
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        # Open with explicit mode for security
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())  # Ensure data is written to disk
        except:
            # If fdopen succeeded, fd is now owned by the file object
            # and will be closed when we exit the with block
            raise

        # Atomically replace original file
        os.replace(tmp_path, path)

        # Ensure permissions are set (in case umask interfered)
        os.chmod(path, mode)
    except:
        # Clean up temp file on error
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def parse_sqlite_timestamp_to_local(timestamp) -> datetime:
    """
    Parse a SQLite timestamp (string or datetime) and convert to local naive datetime.

    SQLite CURRENT_TIMESTAMP returns UTC timestamps. This function handles:
    - String timestamps in ISO format (with or without 'Z' suffix)
    - Datetime objects (naive or timezone-aware)

    Returns a naive datetime in local timezone for consistent display.

    Args:
        timestamp: SQLite timestamp (string or datetime object)

    Returns:
        Naive datetime in local timezone
    """
    if isinstance(timestamp, str):
        # Parse ISO format string, handling 'Z' suffix
        dt_utc = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        # Ensure it's treated as UTC if naive
        if dt_utc.tzinfo is None:
            dt_utc = dt_utc.replace(tzinfo=timezone.utc)
        # Convert to local time and make naive
        return dt_utc.astimezone().replace(tzinfo=None)
    else:
        # Already a datetime object, return as-is
        return timestamp


def is_session_alive(session: Dict) -> bool:
    """
    Multi-factor liveness check using process fingerprinting.
    Verifies PID, create_time, and exe path to prevent false positives from PID reuse.
    """
    debug = os.environ.get('DEBUG_SESSIONS') == '1'

    try:
        proc = psutil.Process(session['pid'])

        # Check 1: Process is running
        if not proc.is_running():
            if debug: print(f"[DEBUG] PID {session['pid']}: not running")
            return False

        # Check 2: Verify process start time matches (prevents PID reuse)
        if session.get('proc_start_time'):
            proc_start_time = proc.create_time()
            stored_start_time = session['proc_start_time']

            # Allow 1 second tolerance for floating point comparison
            if abs(proc_start_time - stored_start_time) >= 1.0:
                if debug:
                    print(f"[DEBUG] PID {session['pid']}: start time mismatch "
                          f"(proc={proc_start_time}, stored={stored_start_time})")
                return False

        # Check 3: Verify executable path matches (optional, may not always be accessible)
        if session.get('exe'):
            try:
                proc_exe = proc.exe()
                if proc_exe != session['exe']:
                    if debug:
                        print(f"[DEBUG] PID {session['pid']}: exe mismatch "
                              f"(proc={proc_exe}, stored={session['exe']})")
                    return False
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                # Can't access exe, skip this check
                pass

        if debug: print(f"[DEBUG] PID {session['pid']}: ALIVE ✓")
        return True

    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired, ValueError) as e:
        if debug: print(f"[DEBUG] PID {session['pid']}: exception {e}")
        return False


def cleanup_dead_sessions(db: 'Database'):
    """
    Mark dead sessions as ended and update last_checked for alive sessions.
    Called before any session-aware command to maintain accurate state.
    """
    active_sessions = db.get_active_sessions()

    for session in active_sessions:
        if is_session_alive(dict(session)):
            # Update last checked timestamp for alive sessions
            db.update_session_last_checked(session['session_id'])
        else:
            # Mark dead sessions as ended
            db.mark_session_ended(session['session_id'])


class Database:
    """SQLite database manager for account and usage data"""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.conn = None
        self.init_db()

    def init_db(self):
        """Initialize database schema"""
        # Ensure database directory exists with secure permissions
        C2SWITCHER_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(C2SWITCHER_DIR, 0o700)
        except OSError:
            pass  # Best effort

        self.conn = sqlite3.connect(str(self.db_path), timeout=5)
        self.conn.row_factory = sqlite3.Row

        # Set DB file permissions to 0o600 (matches Claude Code's .credentials.json)
        try:
            os.chmod(self.db_path, 0o600)
        except (FileNotFoundError, OSError):
            pass  # Best effort

        # Enable important pragmas
        self.conn.execute("PRAGMA foreign_keys = ON")  # Enforce foreign key constraints
        self.conn.execute("PRAGMA journal_mode=WAL")  # WAL mode for better concurrency
        self.conn.execute("PRAGMA busy_timeout=5000")  # Wait up to 5s for locks

        cursor = self.conn.cursor()

        # Accounts table
        cursor.execute("""
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
        """)

        # Usage history table
        cursor.execute("""
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
        """)

        # Indexes for usage queries (optimized for ORDER BY queried_at DESC)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_usage_account_queried
            ON usage_history(account_uuid, queried_at DESC)
        """)

        # Sessions table for tracking active Claude instances
        cursor.execute("""
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
        """)

        # Indexes for session queries
        # Composite index for active sessions with ORDER BY created_at DESC
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_sessions_active_created
            ON sessions(created_at DESC)
            WHERE ended_at IS NULL
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_sessions_account
            ON sessions(account_uuid)
        """)

        self.conn.commit()

    def get_next_index(self) -> int:
        """Get next available account index"""
        cursor = self.conn.cursor()
        cursor.execute("SELECT MAX(index_num) FROM accounts")
        result = cursor.fetchone()[0]
        return 0 if result is None else result + 1

    def add_account(self, profile: Dict, credentials: Dict, nickname: Optional[str] = None) -> int:
        """Add or update an account"""
        account = profile.get("account", {})
        org = profile.get("organization", {})
        uuid = account.get("uuid")

        if not uuid:
            raise ValueError("Invalid profile data: missing account UUID")

        # Use transaction to ensure atomicity
        with self.conn:
            cursor = self.conn.cursor()

            # Check if account exists
            cursor.execute("SELECT id, index_num FROM accounts WHERE uuid = ?", (uuid,))
            existing = cursor.fetchone()

            if existing:
                # Update existing account
                cursor.execute("""
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
                """, (
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
                    uuid
                ))
                return existing[1]  # Return existing index
            else:
                # Insert new account
                index_num = self.get_next_index()
                cursor.execute("""
                    INSERT INTO accounts (
                        uuid, index_num, nickname, email, full_name, display_name,
                        has_claude_max, has_claude_pro, org_uuid, org_name, org_type,
                        billing_type, rate_limit_tier, credentials_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
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
                    json.dumps(credentials)
                ))
                return index_num

    def add_usage(self, account_uuid: str, usage_data: Dict):
        """Add usage data to history"""
        cursor = self.conn.cursor()

        five_hour = usage_data.get("five_hour", {})
        seven_day = usage_data.get("seven_day", {})
        seven_day_opus = usage_data.get("seven_day_opus", {})

        cursor.execute("""
            INSERT INTO usage_history (
                account_uuid, five_hour_utilization, five_hour_resets_at,
                seven_day_utilization, seven_day_resets_at,
                seven_day_opus_utilization, seven_day_opus_resets_at,
                raw_response
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            account_uuid,
            five_hour.get("utilization") if five_hour else None,
            five_hour.get("resets_at") if five_hour else None,
            seven_day.get("utilization") if seven_day else None,
            seven_day.get("resets_at") if seven_day else None,
            seven_day_opus.get("utilization") if seven_day_opus else None,
            seven_day_opus.get("resets_at") if seven_day_opus else None,
            json.dumps(usage_data)
        ))
        self.conn.commit()

    def get_recent_usage(self, account_uuid: str, max_age_seconds: int = 30) -> Optional[Dict]:
        """Get recent usage data if available (within max_age_seconds)"""
        cursor = self.conn.cursor()

        # Use SQLite's datetime functions for proper comparison
        cursor.execute("""
            SELECT raw_response, queried_at
            FROM usage_history
            WHERE account_uuid = ?
            AND datetime(queried_at) > datetime('now', ? || ' seconds')
            ORDER BY queried_at DESC LIMIT 1
        """, (account_uuid, f'-{max_age_seconds}'))

        row = cursor.fetchone()
        if row:
            return json.loads(row[0])
        return None

    def get_all_accounts(self) -> List[sqlite3.Row]:
        """Get all accounts"""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT * FROM accounts ORDER BY index_num
        """)
        return cursor.fetchall()

    def get_account_by_identifier(self, identifier: str) -> Optional[sqlite3.Row]:
        """Get account by index, nickname, email, or uuid"""
        cursor = self.conn.cursor()

        # Try as index
        if identifier.isdigit():
            cursor.execute("SELECT * FROM accounts WHERE index_num = ?", (int(identifier),))
            row = cursor.fetchone()
            if row:
                return row

        # Try as nickname, email, or uuid
        cursor.execute("""
            SELECT * FROM accounts
            WHERE nickname = ? OR email = ? OR uuid = ?
        """, (identifier, identifier, identifier))
        return cursor.fetchone()

    def get_latest_usage_for_all_accounts(self) -> Dict[str, Dict]:
        """Get latest usage for all accounts"""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT
                uh.account_uuid,
                uh.raw_response,
                uh.queried_at
            FROM usage_history uh
            INNER JOIN (
                SELECT account_uuid, MAX(queried_at) as max_time
                FROM usage_history
                GROUP BY account_uuid
            ) latest ON uh.account_uuid = latest.account_uuid
                AND uh.queried_at = latest.max_time
        """)

        result = {}
        for row in cursor.fetchall():
            result[row[0]] = {
                "data": json.loads(row[1]),
                "queried_at": row[2]
            }
        return result

    def create_session(self, session_id: str, pid: int, parent_pid: Optional[int],
                      proc_start_time: float, exe: str, cmdline: str, cwd: str):
        """Create a new session record with process fingerprinting"""
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO sessions (session_id, pid, parent_pid, proc_start_time, exe, cmdline, cwd)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (session_id, pid, parent_pid, proc_start_time, exe, cmdline, cwd))
        self.conn.commit()

    def get_session(self, session_id: str) -> Optional[sqlite3.Row]:
        """Get session by ID"""
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
        return cursor.fetchone()

    def get_session_account(self, session_id: str) -> Optional[sqlite3.Row]:
        """Get the account assigned to a session"""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT a.* FROM accounts a
            JOIN sessions s ON s.account_uuid = a.uuid
            WHERE s.session_id = ? AND s.ended_at IS NULL
        """, (session_id,))
        return cursor.fetchone()

    def get_active_sessions(self) -> List[sqlite3.Row]:
        """Get all active sessions (not ended)"""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT * FROM sessions
            WHERE ended_at IS NULL
            ORDER BY created_at DESC
        """)
        return cursor.fetchall()

    def count_active_sessions(self, account_uuid: str) -> int:
        """Count active sessions for a given account"""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT COUNT(*) FROM sessions
            WHERE account_uuid = ? AND ended_at IS NULL
        """, (account_uuid,))
        return cursor.fetchone()[0]

    def count_recent_sessions(self, account_uuid: str, minutes: int = 5) -> int:
        """Count sessions that started in the last N minutes (for rate limiting)"""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT COUNT(*) FROM sessions
            WHERE account_uuid = ?
              AND datetime(created_at) >= datetime('now', '-' || ? || ' minutes')
        """, (account_uuid, minutes))
        return cursor.fetchone()[0]

    def assign_session_to_account(self, session_id: str, account_uuid: str):
        """Assign a session to an account"""
        cursor = self.conn.cursor()
        cursor.execute("""
            UPDATE sessions
            SET account_uuid = ?
            WHERE session_id = ?
        """, (account_uuid, session_id))
        self.conn.commit()

    def mark_session_ended(self, session_id: str):
        """Mark a session as ended"""
        cursor = self.conn.cursor()
        cursor.execute("""
            UPDATE sessions
            SET ended_at = CURRENT_TIMESTAMP
            WHERE session_id = ?
        """, (session_id,))
        self.conn.commit()

    def update_session_last_checked(self, session_id: str):
        """Update last_checked_alive timestamp"""
        cursor = self.conn.cursor()
        cursor.execute("""
            UPDATE sessions
            SET last_checked_alive = CURRENT_TIMESTAMP
            WHERE session_id = ?
        """, (session_id,))
        self.conn.commit()

    def get_session_history(self, min_duration_seconds: int = 5, limit: int = 50) -> List[sqlite3.Row]:
        """Get historical sessions (ended sessions with minimum duration)"""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT *,
                   (julianday(ended_at) - julianday(created_at)) * 86400 as duration_seconds
            FROM sessions
            WHERE ended_at IS NOT NULL
              AND (julianday(ended_at) - julianday(created_at)) * 86400 >= ?
            ORDER BY ended_at DESC
            LIMIT ?
        """, (min_duration_seconds, limit))
        return cursor.fetchall()

    def get_usage_before(self, account_uuid: str, timestamp: str) -> Optional[Dict]:
        """Get the closest usage snapshot before a given timestamp"""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT raw_response, queried_at
            FROM usage_history
            WHERE account_uuid = ? AND queried_at <= ?
            ORDER BY queried_at DESC
            LIMIT 1
        """, (account_uuid, timestamp))
        row = cursor.fetchone()
        if row:
            return {
                "data": json.loads(row[0]),
                "queried_at": row[1]
            }
        return None

    def get_usage_after(self, account_uuid: str, timestamp: str) -> Optional[Dict]:
        """Get the closest usage snapshot after a given timestamp"""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT raw_response, queried_at
            FROM usage_history
            WHERE account_uuid = ? AND queried_at >= ?
            ORDER BY queried_at ASC
            LIMIT 1
        """, (account_uuid, timestamp))
        row = cursor.fetchone()
        if row:
            return {
                "data": json.loads(row[0]),
                "queried_at": row[1]
            }
        return None

    def close(self):
        """Close database connection"""
        if self.conn:
            self.conn.close()

    def __enter__(self):
        """Context manager entry"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - automatically close connection"""
        self.close()
        return False  # Don't suppress exceptions


def load_headers_config() -> Dict[str, str]:
    """
    Load headers configuration from .c2switcher/headers.json.
    Creates default configuration if it doesn't exist.

    Returns:
        Dictionary of header key-value pairs
    """
    default_headers = {
        "accept": "application/json, text/plain, */*",
        "accept-encoding": "gzip, compress, deflate, br",
        "anthropic-beta": "oauth-2025-04-20",
        "content-type": "application/json",
        "user-agent": "claude-code/2.0.20",
        "connection": "keep-alive"
    }

    # Create config directory if it doesn't exist
    HEADERS_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    # If config file doesn't exist, create it with defaults
    if not HEADERS_PATH.exists():
        try:
            atomic_write_json(HEADERS_PATH, default_headers, preserve_permissions=False)
        except Exception:
            # Fall back to defaults if can't write config
            return default_headers

    # Load configuration
    try:
        with open(HEADERS_PATH, 'r') as f:
            config = json.load(f)
            # Merge with defaults (config overrides defaults)
            headers = default_headers.copy()
            headers.update(config)
            return headers
    except Exception:
        # Fall back to defaults on any error
        return default_headers


class ClaudeAPI:
    """Claude API client for profile and usage endpoints"""

    BASE_URL = "https://api.anthropic.com/api/oauth"
    # Timeout: (connect timeout, read timeout) in seconds
    TIMEOUT = (5, 20)

    @staticmethod
    def _get_headers(token: str) -> Dict[str, str]:
        """
        Get common API headers with authorization token.

        Loads base headers from .c2switcher/headers.json and adds authorization.
        """
        headers = load_headers_config()
        headers["authorization"] = f"Bearer {token}"
        return headers

    @staticmethod
    def get_profile(token: str) -> Dict:
        """Get profile information"""
        response = requests.get(
            f"{ClaudeAPI.BASE_URL}/profile",
            headers=ClaudeAPI._get_headers(token),
            timeout=ClaudeAPI.TIMEOUT
        )
        response.raise_for_status()
        return response.json()

    @staticmethod
    def get_usage(token: str) -> Dict:
        """Get usage information"""
        response = requests.get(
            f"{ClaudeAPI.BASE_URL}/usage",
            headers=ClaudeAPI._get_headers(token),
            timeout=ClaudeAPI.TIMEOUT
        )
        response.raise_for_status()
        return response.json()


class SandboxEnvironment:
    """
    Context manager for isolated Claude Code sandbox environment.

    Creates a temporary HOME directory with Claude configuration inherited
    from the real HOME to avoid prompts for terminal theme and other settings.
    Automatically cleans up on exit.

    Usage:
        with SandboxEnvironment(account_uuid, credentials) as env:
            subprocess.run(["claude", "..."], env=env)
    """

    def __init__(self, account_uuid: str, credentials: Dict, account_info: Optional[Dict] = None):
        """
        Initialize sandbox environment.

        Args:
            account_uuid: Unique account identifier
            credentials: Claude credentials dictionary
            account_info: Optional account metadata (email, org_uuid, etc.) for .claude.json
        """
        self.account_uuid = account_uuid
        self.credentials = credentials
        self.account_info = account_info
        self.temp_home = None
        self.temp_claude_dir = None
        self.temp_creds_path = None
        self.env = None

    def __enter__(self) -> Dict[str, str]:
        """
        Set up sandbox environment.

        Returns:
            Environment dictionary with sandboxed HOME
        """
        # Create per-account temporary directory
        self.temp_home = Path.home() / ".c2switcher" / "tmp" / self.account_uuid
        self.temp_claude_dir = self.temp_home / ".claude"
        self.temp_claude_dir.mkdir(parents=True, exist_ok=True)
        self.temp_creds_path = self.temp_claude_dir / ".credentials.json"

        # Set secure permissions on sandbox directories
        try:
            os.chmod(self.temp_home, 0o700)
            os.chmod(self.temp_claude_dir, 0o700)
        except OSError:
            pass  # Best effort

        # Write credentials to sandbox
        atomic_write_json(self.temp_creds_path, self.credentials, preserve_permissions=False)

        # Create .claude.json from template with oauthAccount field
        sandbox_claude_json = self.temp_home / ".claude.json"

        try:
            # Base template
            claude_json = {
                "numStartups": 4,
                "installMethod": "unknown",
                "autoUpdates": False,
                "tipsHistory": {
                    "git-worktrees": 0
                },
                "cachedStatsigGates": {
                    "tengu_disable_bypass_permissions_mode": False,
                    "tengu_tool_pear": False
                },
                "cachedDynamicConfigs": {
                    "tengu-top-of-feed-tip": {
                        "tip": "",
                        "color": "dim"
                    }
                },
                "fallbackAvailableWarningThreshold": 0.5,
                "firstStartTime": f"{datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}",
                "sonnet45MigrationComplete": True,
                "changelogLastFetched": int(time.time() * 1000) - random.randint(3600000, 86400000),
                "claudeCodeFirstTokenDate": f"{(datetime.now(timezone.utc) - timedelta(days=random.randint(30, 180))).isoformat()}Z",
                "hasCompletedOnboarding": True,
                "lastOnboardingVersion": "2.0.25",
                "hasOpusPlanDefault": False,
                "lastReleaseNotesSeen": "2.0.25",
                "subscriptionNoticeCount": 0,
                "hasAvailableSubscription": False,
                "bypassPermissionsModeAccepted": True
            }

            # Add oauthAccount field with account-specific info if available
            if self.account_info:
                claude_json["oauthAccount"] = {
                    "accountUuid": self.account_info.get("uuid", self.account_uuid),
                    "emailAddress": self.account_info.get("email", ""),
                    "organizationUuid": self.account_info.get("org_uuid", ""),
                    "displayName": self.account_info.get("display_name", ""),
                    "organizationBillingType": self.account_info.get("billing_type", ""),
                    "organizationRole": "admin",  # Default since not stored in DB
                    "workspaceRole": None,
                    "organizationName": self.account_info.get("org_name", "")
                }

            atomic_write_json(sandbox_claude_json, claude_json, preserve_permissions=False)

            # Set secure permissions on file
            os.chmod(sandbox_claude_json, 0o600)
        except (PermissionError, OSError) as e:
            # Log warning but continue - not critical for core functionality
            pass

        # Always write settings.json template
        sandbox_settings = self.temp_claude_dir / "settings.json"

        try:
            settings = {
                "$schema": "https://json.schemastore.org/claude-code-settings.json",
                "alwaysThinkingEnabled": True
            }
            atomic_write_json(sandbox_settings, settings, preserve_permissions=False)
            os.chmod(sandbox_settings, 0o600)
        except (PermissionError, OSError):
            pass  # Best effort

        # Create environment with sandboxed HOME
        self.env = os.environ.copy()
        self.env["HOME"] = str(self.temp_home.resolve())
        self.env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"

        return self.env

    def __exit__(self, exc_type, exc_val, exc_tb):
        """
        Clean up sandbox environment.

        Removes the temporary directory tree.
        """
        if self.temp_home and self.temp_home.exists():
            try:
                shutil.rmtree(self.temp_home)
            except Exception as e:
                # Log warning but don't fail - cleanup is best-effort
                console.print(f"[yellow]Warning: Failed to clean up sandbox directory {self.temp_home}: {e}[/yellow]")

        return False  # Don't suppress exceptions

    def get_refreshed_credentials(self) -> Dict:
        """
        Read and return refreshed credentials from sandbox.

        Returns:
            Updated credentials dictionary, or original credentials if file was deleted
        """
        try:
            with open(self.temp_creds_path, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            # Claude Code might have deleted the credentials file if auth failed
            # Return the original credentials in this case
            return self.credentials


def refresh_token_direct(credentials_json: str) -> Optional[Dict]:
    """
    Refresh token directly using Anthropic's OAuth endpoint.

    Args:
        credentials_json: JSON string of credentials containing refresh token

    Returns:
        Updated credentials dict with new tokens, or None if refresh failed
    """
    creds = json.loads(credentials_json)
    oauth = creds.get("claudeAiOauth", {})
    refresh_token = oauth.get("refreshToken")

    if not refresh_token:
        return None

    try:
        response = requests.post(
            "https://console.anthropic.com/v1/oauth/token",
            json={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
            },
            timeout=10
        )

        if response.status_code == 200:
            token_data = response.json()

            # Update credentials with new tokens
            new_creds = copy.deepcopy(creds)
            new_creds["claudeAiOauth"]["accessToken"] = token_data["access_token"]
            new_creds["claudeAiOauth"]["refreshToken"] = token_data.get("refresh_token", refresh_token)
            new_creds["claudeAiOauth"]["expiresAt"] = int(time.time() * 1000) + (token_data.get("expires_in", 3600) * 1000)

            return new_creds
        else:
            console.print(f"[yellow]Direct token refresh failed: {response.status_code}[/yellow]")
            return None

    except Exception as e:
        console.print(f"[yellow]Direct token refresh error: {e}[/yellow]")
        return None


def _refresh_token_sandbox(credentials_json: str, account_uuid: Optional[str] = None, account_info: Optional[Dict] = None) -> Dict:
    """
    Internal function: Refresh token using a sandboxed per-account HOME directory.

    This prevents interfering with any running Claude instances by using
    a temporary directory that won't affect the global ~/.claude/.credentials.json.
    """
    creds = json.loads(credentials_json)

    # Store original expiry for comparison later
    expires_at = creds.get("claudeAiOauth", {}).get("expiresAt", 0)

    # Make a copy and fake the expiry to force Claude Code to refresh
    creds_to_refresh = copy.deepcopy(creds)
    fake_expiry = int(time.time() * 1000) + 60000  # 60 seconds from now
    creds_to_refresh["claudeAiOauth"]["expiresAt"] = fake_expiry

    # Use bootstrap UUID if not provided (for initial add)
    if account_uuid is None:
        import hashlib
        creds_hash = hashlib.sha256(credentials_json.encode()).hexdigest()[:16]
        account_uuid = f"bootstrap-{creds_hash}"

    # Use sandboxed environment with automatic cleanup
    # Pass credentials (possibly with faked expiry if force=True)
    sandbox = SandboxEnvironment(account_uuid, creds_to_refresh, account_info)
    with sandbox as env:
        # 10% chance to skip /status and go directly to fallback
        use_fallback = random.random() < 0.1

        if not use_fallback:
            # Try /status command first (doesn't consume usage)
            try:
                result = subprocess.run(
                    ["claude", "-p", "/status", "--verbose", "--output-format=json"],
                    timeout=30,
                    capture_output=True,
                    check=False,
                    env=env
                )

                # Read credentials to check if token was refreshed
                refreshed_creds = sandbox.get_refreshed_credentials()

                # Check if token was actually refreshed
                new_expires_at = refreshed_creds.get("claudeAiOauth", {}).get("expiresAt", 0)
                if new_expires_at > expires_at:
                    # Token successfully refreshed
                    return refreshed_creds

                # If /status didn't refresh, fall back to actual prompt
                console.print("[yellow]Status check didn't refresh token, using fallback...[/yellow]")

            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass

        # Fallback: Run with actual prompt
        try:
            subprocess.run(
                ["claude", "-p", "hi", "--model", "haiku"],
                timeout=30,
                capture_output=True,
                check=False,
                env=env
            )
        except subprocess.TimeoutExpired:
            pass
        except FileNotFoundError:
            console.print("[red]Error: 'claude' command not found. Please ensure Claude Code is installed.[/red]")
            raise

        # Read refreshed credentials
        refreshed_creds = sandbox.get_refreshed_credentials()

        # Check if refresh was successful
        final_expires_at = refreshed_creds.get("claudeAiOauth", {}).get("expiresAt", 0)
        if final_expires_at <= expires_at:
            # Token wasn't refreshed - likely revoked or invalid
            console.print("[red]Error: Failed to refresh token. The credentials may be revoked or invalid.[/red]")
            raise ValueError(
                "Token refresh failed after multiple attempts. "
                "Please re-authenticate by logging in to Claude Code with this account."
            )

        return refreshed_creds


def refresh_token(credentials_json: str, account_uuid: Optional[str] = None, account_info: Optional[Dict] = None, force: bool = False) -> Dict:
    """
    Centralized token refresh function.

    Checks if token is still valid (within 10 minute buffer), and if not:
    1. Tries direct OAuth refresh first (fast)
    2. Falls back to Claude Code sandbox method if direct fails

    Args:
        credentials_json: JSON string of credentials to refresh
        account_uuid: Account UUID for sandbox directory. If None, uses a bootstrap hash.
        account_info: Optional account metadata (email, org_uuid, etc.) for .claude.json patching
        force: If True, force refresh regardless of expiry time

    Returns:
        Updated credentials dict with fresh tokens
    """
    # Parse credentials
    creds = json.loads(credentials_json)

    # Check if token is still valid (with 10 minute buffer)
    expires_at = creds.get("claudeAiOauth", {}).get("expiresAt", 0)
    if not force and expires_at - 600000 > int(time.time() * 1000):
        # Token not expired, return as-is
        return creds

    # Try direct OAuth token refresh first (faster and cleaner)
    console.print("[yellow]Refreshing token...[/yellow]")
    refreshed = refresh_token_direct(credentials_json)
    if refreshed:
        console.print("[green]Token refreshed successfully[/green]")
        return refreshed

    # Fallback to sandbox method if direct refresh fails
    console.print("[yellow]Direct refresh failed, using Claude Code sandbox method...[/yellow]")
    return _refresh_token_sandbox(credentials_json, account_uuid, account_info)


def get_account_usage(db: Database, account_uuid: str, credentials_json: str, force: bool = False) -> Dict:
    """Get usage for an account, using cache if available"""
    # Check cache first (5 minute cache to reduce API calls and token refreshes)
    if not force:
        cached = db.get_recent_usage(account_uuid, max_age_seconds=300)
        if cached:
            return cached

    # Fetch account info for sandbox .claude.json patching
    cursor = db.conn.cursor()
    cursor.execute("SELECT uuid, email, org_uuid, display_name, billing_type, org_name FROM accounts WHERE uuid = ?", (account_uuid,))
    row = cursor.fetchone()
    account_info = None
    if row:
        account_info = {
            "uuid": row[0],
            "email": row[1],
            "org_uuid": row[2],
            "display_name": row[3],
            "billing_type": row[4],
            "org_name": row[5]
        }

    # Refresh token if needed (refresh_token checks expiry internally)
    refreshed_creds = refresh_token(credentials_json, account_uuid, account_info)
    token = refreshed_creds.get("claudeAiOauth", {}).get("accessToken")

    if not token:
        raise ValueError("No access token found in credentials")

    # Fetch usage
    usage = ClaudeAPI.get_usage(token)

    # Save to database
    db.add_usage(account_uuid, usage)

    # Update credentials if refreshed
    if refreshed_creds != json.loads(credentials_json):
        cursor = db.conn.cursor()
        cursor.execute(
            "UPDATE accounts SET credentials_json = ?, updated_at = CURRENT_TIMESTAMP WHERE uuid = ?",
            (json.dumps(refreshed_creds), account_uuid)
        )
        db.conn.commit()

    return usage


def select_account_with_load_balancing(db: Database, session_id: Optional[str] = None) -> Optional[Dict]:
    """
    Select optimal account using tier-based filtering and load balancing.

    Eligibility tiers (in priority order):
    1. Tier 1: Opus <90% used (>=10% Opus headroom)
    2. Tier 2: Opus exhausted, but overall <90% used (>=10% overall headroom)
    3. Excluded: Any account at 100% on either metric

    Load balancing considers both active sessions AND recent usage (last 5min)
    for better rate limit distribution, even for short-lived commands.

    Score = utilization + (active_sessions x 15) + (recent_sessions x 5)
    """
    # Clean up dead sessions first
    cleanup_dead_sessions(db)

    # If session ID provided, check if already assigned
    if session_id:
        existing = db.get_session_account(session_id)
        if existing:
            return {
                "account": existing,
                "reused": True
            }

    # Get all accounts
    accounts = db.get_all_accounts()
    if not accounts:
        return None

    # Fetch usage for all accounts and score them
    tier1_accounts = []  # Opus available (>10% remaining = <90% used)
    tier2_accounts = []  # Overall available (>10% remaining = <90% used)

    for acc in accounts:
        try:
            usage = get_account_usage(db, acc["uuid"], acc["credentials_json"])
            seven_day_opus = usage.get("seven_day_opus", {})
            seven_day = usage.get("seven_day", {})

            opus_usage = seven_day_opus.get("utilization") if seven_day_opus else 100
            overall_usage = seven_day.get("utilization") if seven_day else 100

            # Handle None values
            opus_usage = opus_usage if opus_usage is not None else 100
            overall_usage = overall_usage if overall_usage is not None else 100

            # Exclude accounts at 100% (completely maxed out)
            if opus_usage >= 100 or overall_usage >= 100:
                continue

            # Count both active sessions AND recent sessions (for rate limiting)
            active_sessions = db.count_active_sessions(acc['uuid'])
            recent_sessions = db.count_recent_sessions(acc['uuid'], minutes=5)

            # Session penalties (load balancing weights):
            # - 15 points per active session: Accounts with active sessions are heavily
            #   penalized to distribute load and avoid rate limits. A 15-point penalty
            #   means we prefer accounts with up to 15% more usage over ones with active sessions.
            # - 5 points per recent session (last 5min): Lighter penalty for rate limiting
            #   prevention. Distributes burst load without being too aggressive.
            #
            # Example: Account A at 50% usage with 2 active sessions (score=80) vs
            #          Account B at 70% usage with 0 sessions (score=70) -> B is selected.
            session_penalty = (active_sessions * 15) + (recent_sessions * 5)

            # Determine tier and calculate score
            if opus_usage < 90:
                # Tier 1: Opus available
                base_score = opus_usage
                final_score = base_score + session_penalty
                tier1_accounts.append({
                    "account": acc,
                    "tier": 1,
                    "score": final_score,
                    "opus_usage": opus_usage,
                    "overall_usage": overall_usage,
                    "active_sessions": active_sessions,
                    "recent_sessions": recent_sessions
                })
            elif overall_usage < 90:
                # Tier 2: Opus exhausted but overall available
                base_score = overall_usage
                final_score = base_score + session_penalty
                tier2_accounts.append({
                    "account": acc,
                    "tier": 2,
                    "score": final_score,
                    "opus_usage": opus_usage,
                    "overall_usage": overall_usage,
                    "active_sessions": active_sessions,
                    "recent_sessions": recent_sessions
                })
            # else: Skip accounts with <10% headroom on both metrics

        except Exception as e:
            # Skip accounts that fail to fetch usage
            console.print(f"[yellow]Warning: Could not fetch usage for {acc['email']}: {e}[/yellow]")
            continue

    # Select from best available tier
    if tier1_accounts:
        # Sort by score (lower is better)
        tier1_accounts.sort(key=lambda x: x['score'])
        selected = tier1_accounts[0]
    elif tier2_accounts:
        tier2_accounts.sort(key=lambda x: x['score'])
        selected = tier2_accounts[0]
    else:
        return None  # No accounts available

    # Assign session to selected account if session_id provided
    if session_id:
        db.assign_session_to_account(session_id, selected['account']['uuid'])

    return {
        "account": selected['account'],
        "tier": selected['tier'],
        "score": selected['score'],
        "opus_usage": selected['opus_usage'],
        "overall_usage": selected['overall_usage'],
        "active_sessions": selected['active_sessions'],
        "recent_sessions": selected.get('recent_sessions', 0),
        "reused": False
    }


# CLI Commands

@click.group()
def cli():
    """Claude Code Account Switcher - Manage multiple Claude Code accounts"""
    pass


@cli.command()
@click.option("--nickname", "-n", help="Optional nickname for the account")
@click.option("--creds-file", "-f", type=click.Path(exists=True), help="Path to credentials JSON file")
def add(nickname: Optional[str], creds_file: Optional[str]):
    """Add a new account from credentials file or current .credentials.json"""
    acquire_lock()  # Lock for database write
    db = Database()

    try:
        # Load credentials
        if creds_file:
            with open(creds_file, 'r') as f:
                credentials = json.load(f)
        else:
            if not CREDENTIALS_PATH.exists():
                console.print(f"[red]Error: {CREDENTIALS_PATH} not found[/red]")
                console.print("[yellow]Please specify a credentials file with --creds-file[/yellow]")
                return
            with open(CREDENTIALS_PATH, 'r') as f:
                credentials = json.load(f)

        # Check if token is expired, refresh first if needed (before get_profile)
        expires_at = credentials.get("claudeAiOauth", {}).get("expiresAt", 0)
        if expires_at <= int(time.time() * 1000):
            # Token expired - refresh with bootstrap UUID before getting profile
            credentials = refresh_token(json.dumps(credentials), account_uuid=None)

        # Get access token
        token = credentials.get("claudeAiOauth", {}).get("accessToken")
        if not token:
            console.print("[red]Error: No access token found in credentials[/red]")
            return

        # Get profile to extract UUID
        try:
            with console.status("[bold green]Fetching account profile..."):
                profile = ClaudeAPI.get_profile(token)
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401:
                # Token invalid or expired - try refreshing again
                console.print("[yellow]Token rejected, attempting refresh...[/yellow]")
                credentials = refresh_token(json.dumps(credentials), account_uuid=None)
                token = credentials.get("claudeAiOauth", {}).get("accessToken")
                with console.status("[bold green]Retrying profile fetch..."):
                    profile = ClaudeAPI.get_profile(token)
            else:
                raise

        # Get account UUID for future refreshes
        account_uuid = profile.get("account", {}).get("uuid")
        if not account_uuid:
            console.print("[red]Error: No account UUID in profile[/red]")
            return

        # Final refresh with real UUID to update sandbox directory
        credentials = refresh_token(json.dumps(credentials), account_uuid)

        # Add to database
        index = db.add_account(profile, credentials, nickname)

        account = profile.get("account", {})
        console.print(Panel(
            f"[green]✓[/green] Account added successfully\n\n"
            f"Index: [bold]{index}[/bold]\n"
            f"Email: [bold]{account.get('email')}[/bold]\n"
            f"Name: {account.get('full_name')}\n"
            f"Nickname: {nickname or '[dim]none[/dim]'}",
            title="Account Added",
            border_style="green"
        ))

    except Exception as e:
        console.print(f"[red]Error adding account: {e}[/red]")
    finally:
        db.close()


@cli.command(name="ls")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
def list_accounts_cmd(output_json: bool):
    """List all accounts"""
    db = Database()

    try:
        accounts = db.get_all_accounts()

        if output_json:
            result = []
            for acc in accounts:
                result.append({
                    "index": acc["index_num"],
                    "nickname": acc["nickname"],
                    "email": acc["email"],
                    "full_name": acc["full_name"],
                    "display_name": acc["display_name"],
                    "has_claude_max": bool(acc["has_claude_max"]),
                    "has_claude_pro": bool(acc["has_claude_pro"]),
                    "org_type": acc["org_type"],
                    "rate_limit_tier": acc["rate_limit_tier"],
                })
            print(json.dumps(result, indent=2))
        else:
            if not accounts:
                console.print("[yellow]No accounts found. Add one with 'c2switcher add'[/yellow]")
                return

            table = Table(title="Claude Code Accounts", box=box.ROUNDED)
            table.add_column("Index", style="cyan", justify="center")
            table.add_column("Nickname", style="magenta")
            table.add_column("Email", style="green")
            table.add_column("Name", style="blue")
            table.add_column("Type", justify="center")
            table.add_column("Tier", style="yellow")

            for acc in accounts:
                account_type = "Max" if acc["has_claude_max"] else "Pro" if acc["has_claude_pro"] else "Free"
                type_color = "green" if acc["has_claude_max"] else "blue" if acc["has_claude_pro"] else "dim"

                table.add_row(
                    str(acc["index_num"]),
                    acc["nickname"] or "[dim]--[/dim]",
                    acc["email"],
                    acc["display_name"] or acc["full_name"] or "[dim]--[/dim]",
                    f"[{type_color}]{account_type}[/{type_color}]",
                    acc["rate_limit_tier"] or "[dim]--[/dim]"
                )

            console.print(table)

    finally:
        db.close()


@cli.command()
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.option("--force", is_flag=True, help="Force refresh (ignore cache)")
def usage(output_json: bool, force: bool):
    """List usage across all accounts with session distribution"""
    acquire_lock()  # Lock for read-modify-write (fetch API + write DB)
    db = Database()

    try:
        # Clean up dead sessions first
        cleanup_dead_sessions(db)

        accounts = db.get_all_accounts()

        if not accounts:
            console.print("[yellow]No accounts found. Add one with 'c2switcher add'[/yellow]")
            return

        # Get session counts per account
        session_counts = {}
        for acc in accounts:
            session_counts[acc['uuid']] = db.count_active_sessions(acc['uuid'])

        usage_data = []

        for acc in accounts:
            try:
                display_name = acc['nickname'] or acc['email']
                with console.status(f"[bold green]Fetching usage for {display_name}..."):
                    usage = get_account_usage(db, acc["uuid"], acc["credentials_json"], force=force)

                usage_data.append({
                    "account": acc,
                    "usage": usage,
                    "sessions": session_counts[acc['uuid']]
                })
            except Exception as e:
                display_name = acc['nickname'] or acc['email']
                console.print(f"[red]Error fetching usage for {display_name}: {e}[/red]")
                usage_data.append({
                    "account": acc,
                    "usage": None,
                    "sessions": session_counts[acc['uuid']],
                    "error": str(e)
                })

        if output_json:
            result = []
            for item in usage_data:
                acc = item["account"]
                usage = item["usage"]
                result.append({
                    "index": acc["index_num"],
                    "nickname": acc["nickname"],
                    "email": acc["email"],
                    "usage": usage,
                    "sessions": item["sessions"],
                    "error": item.get("error")
                })
            print(json.dumps(result, indent=2))
        else:
            table = Table(title="Usage Across Accounts", box=box.ROUNDED)
            table.add_column("Index", style="cyan", justify="center")
            table.add_column("Nickname", style="magenta")
            table.add_column("Email", style="green")
            table.add_column("5h", justify="right")
            table.add_column("7d", justify="right")
            table.add_column("7d Opus", justify="right")
            table.add_column("Reset (Rate)", justify="right", no_wrap=True)
            table.add_column("Sessions", style="blue", justify="center")

            for item in usage_data:
                acc = item["account"]
                usage = item["usage"]
                sessions = item["sessions"]

                # Format session count
                if sessions > 0:
                    session_str = f"[blue]{sessions}[/blue]"
                else:
                    session_str = "[dim]0[/dim]"

                if usage is None:
                    table.add_row(
                        str(acc["index_num"]),
                        acc["nickname"] or "[dim]--[/dim]",
                        acc["email"],
                        "[red]Error[/red]",
                        "[red]Error[/red]",
                        "[red]Error[/red]",
                        "[red]Error[/red]",
                        session_str
                    )
                else:
                    five_hour = usage.get("five_hour", {})
                    seven_day = usage.get("seven_day", {})
                    seven_day_opus = usage.get("seven_day_opus", {})

                    def format_usage(val):
                        if val is None:
                            return "[dim]--[/dim]"
                        if val >= 90:
                            return f"[red]{val}%[/red]"
                        elif val >= 70:
                            return f"[yellow]{val}%[/yellow]"
                        else:
                            return f"[green]{val}%[/green]"

                    # Calculate time until 7d reset with usage rate
                    opus_util = seven_day_opus.get("utilization")
                    overall_util = seven_day.get("utilization")
                    reset_time = format_time_until_reset(
                        seven_day.get("resets_at"),
                        opus_util if opus_util is not None else 0,
                        overall_util if overall_util is not None else 0
                    )

                    table.add_row(
                        str(acc["index_num"]),
                        acc["nickname"] or "[dim]--[/dim]",
                        acc["email"],
                        format_usage(five_hour.get("utilization")),
                        format_usage(seven_day.get("utilization")),
                        format_usage(seven_day_opus.get("utilization")),
                        reset_time,
                        session_str
                    )

            console.print(table)

            # Show active sessions summary
            active_sessions = db.get_active_sessions()
            if active_sessions:
                console.print(f"\n[bold]Active Sessions ({len(active_sessions)}):[/bold]")
                for session in active_sessions[:5]:  # Show first 5
                    # Get account email
                    account_email = "[dim]not assigned[/dim]"
                    if session['account_uuid']:
                        acc = db.get_account_by_identifier(session['account_uuid'])
                        if acc:
                            account_email = acc['email']

                    # Format time ago
                    started = session['created_at']
                    started_dt = parse_sqlite_timestamp_to_local(started)

                    time_ago = datetime.now() - started_dt
                    if time_ago.total_seconds() < 60:
                        time_str = f"{int(time_ago.total_seconds())}s ago"
                    elif time_ago.total_seconds() < 3600:
                        time_str = f"{int(time_ago.total_seconds() / 60)}m ago"
                    else:
                        time_str = f"{int(time_ago.total_seconds() / 3600)}h ago"

                    cwd = session['cwd'] or "unknown"
                    if len(cwd) > 35:
                        cwd = "..." + cwd[-32:]

                    console.print(f"  * {account_email} [dim]({cwd}, {time_str})[/dim]")

                if len(active_sessions) > 5:
                    console.print(f"  [dim]... and {len(active_sessions) - 5} more[/dim]")

    finally:
        db.close()


@cli.command()
@click.option("--switch", is_flag=True, help="Actually switch to the optimal account")
@click.option("--session-id", help="Session ID for load balancing and sticky assignment")
@click.option("--token-only", is_flag=True, help="Output only the token to stdout")
def optimal(switch: bool, session_id: Optional[str], token_only: bool):
    """Find the optimal account with load balancing and session stickiness"""
    # Lock if switching credentials OR if assigning a session (DB write)
    if switch or session_id:
        acquire_lock()

    db = Database()

    try:
        # Use new load balancing function
        result = select_account_with_load_balancing(db, session_id)

        if not result:
            console.print("[red]No accounts available (all at capacity or no accounts found)[/red]")
            return

        optimal_acc = result['account']

        # Get and refresh token if needed
        if switch or token_only:
            credentials = json.loads(optimal_acc["credentials_json"])

            # Refresh if needed
            account_info = {
                "uuid": optimal_acc['uuid'],
                "email": optimal_acc['email'],
                "org_uuid": optimal_acc['org_uuid'],
                "display_name": optimal_acc['display_name'],
                "billing_type": optimal_acc['billing_type'],
                "org_name": optimal_acc['org_name']
            }
            refreshed_creds = refresh_token(json.dumps(credentials), optimal_acc['uuid'], account_info)
            token = refreshed_creds.get("claudeAiOauth", {}).get("accessToken")

            if not token:
                console.print("[red]Error: No access token found in credentials[/red]")
                return

            # Update credentials if refreshed
            if refreshed_creds != credentials:
                cursor = db.conn.cursor()
                cursor.execute(
                    "UPDATE accounts SET credentials_json = ?, updated_at = CURRENT_TIMESTAMP WHERE uuid = ?",
                    (json.dumps(refreshed_creds), optimal_acc['uuid'])
                )
                db.conn.commit()

        nickname = optimal_acc['nickname'] or '[dim]none[/dim]'
        masked_email = mask_email(optimal_acc['email'])

        # Build info message
        tier_label = f"Tier {result['tier']}" if 'tier' in result else "N/A"
        session_info = ""

        if 'reused' in result and result['reused']:
            session_info = "\n[cyan]↻ Session reused existing assignment[/cyan]"
        elif 'active_sessions' in result or 'recent_sessions' in result:
            active = result.get('active_sessions', 0)
            recent = result.get('recent_sessions', 0)
            session_info = f"\n[dim]Sessions: {active} active, {recent} recent (5min)[/dim]"

        info_text = (
            f"[green]Optimal Account (={optimal_acc['index_num']}) - {tier_label}[/green]\n\n"
            f"Nickname: [bold]{nickname}[/bold]\n"
            f"Email: [bold]{masked_email}[/bold]\n"
            f"Opus Usage:    {result.get('opus_usage', 0):>3}%\n"
            f"Overall Usage: {result.get('overall_usage', 0):>3}%"
        )

        if 'score' in result:
            info_text += f"\n[dim]Load Score: {result['score']:.1f}[/dim]"

        info_text += session_info

        if token_only:
            # Print info to stderr, token to stdout
            console.print(Panel(
                info_text,
                border_style="green"
            ))
            print(token)
        else:
            # Write credentials file
            CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
            atomic_write_json(CREDENTIALS_PATH, refreshed_creds)

            # Print info to stdout
            console.print(Panel(
                info_text,
                border_style="green"
            ))

            if switch and not session_id:
                console.print("[green]✓[/green] Switched to optimal account")

    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
    finally:
        db.close()


@cli.command()
@click.argument("identifier", required=False)
@click.option("--session-id", help="Session ID for load balancing (when no identifier given)")
@click.option("--token-only", is_flag=True, help="Output only the token to stdout")
def switch(identifier: Optional[str], session_id: Optional[str], token_only: bool):
    """Switch to a specific account by index, nickname, email, or UUID

    If no identifier is given, uses optimal account selection with load balancing.
    """
    if not identifier and not session_id:
        console.print("[red]Error: Must provide either an identifier or --session-id for load balancing[/red]")
        return

    # Lock if switching credentials OR if assigning a session (DB write)
    if not token_only or session_id:
        acquire_lock()

    db = Database()

    try:
        if identifier:
            # Switch to specific account
            account = db.get_account_by_identifier(identifier)

            if not account:
                console.print(f"[red]Account not found: {identifier}[/red]")
                return
        else:
            # Use load balancing
            result = select_account_with_load_balancing(db, session_id)
            if not result:
                console.print("[red]No accounts available (all at capacity or no accounts found)[/red]")
                return
            account = result['account']

        # Get token
        credentials = json.loads(account["credentials_json"])

        # Refresh if needed
        account_info = {
            "uuid": account['uuid'],
            "email": account['email'],
            "org_uuid": account['org_uuid'],
            "display_name": account['display_name'],
            "billing_type": account['billing_type'],
            "org_name": account['org_name']
        }
        refreshed_creds = refresh_token(json.dumps(credentials), account['uuid'], account_info)
        token = refreshed_creds.get("claudeAiOauth", {}).get("accessToken")

        if not token:
            console.print("[red]Error: No access token found in credentials[/red]")
            return

        # Update credentials if refreshed
        if refreshed_creds != credentials:
            cursor = db.conn.cursor()
            cursor.execute(
                "UPDATE accounts SET credentials_json = ?, updated_at = CURRENT_TIMESTAMP WHERE uuid = ?",
                (json.dumps(refreshed_creds), account['uuid'])
            )
            db.conn.commit()

        nickname = account['nickname'] or '[dim]none[/dim]'
        masked_email = mask_email(account['email'])

        panel_content = (
            f"[green]Switched to account (={account['index_num']})[/green]\n\n"
            f"Nickname: [bold]{nickname}[/bold]\n"
            f"Email: [bold]{masked_email}[/bold]"
        )

        if token_only:
            # Print info to stderr, token to stdout
            console.print(Panel(
                panel_content,
                border_style="green"
            ))
            print(token)
        else:
            # Write credentials file
            CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
            atomic_write_json(CREDENTIALS_PATH, refreshed_creds)

            # Print info to stdout
            console.print(Panel(
                panel_content,
                border_style="green"
            ))

    finally:
        db.close()


@cli.command(name="force-refresh")
@click.argument("identifier", required=False)
def force_refresh(identifier: Optional[str]):
    """Force refresh tokens for an account (or all accounts if none specified)"""
    db = Database()

    try:
        if identifier:
            # Refresh specific account
            account = db.get_account_by_identifier(identifier)
            if not account:
                console.print(f"[red]Account not found: {identifier}[/red]")
                return

            accounts_to_refresh = [account]
        else:
            # Refresh all accounts
            cursor = db.conn.cursor()
            cursor.execute("""
                SELECT uuid, email, nickname, credentials_json, index_num, org_uuid, display_name, billing_type, org_name
                FROM accounts
                ORDER BY index_num
            """)
            rows = cursor.fetchall()
            accounts_to_refresh = [
                {
                    'uuid': row[0],
                    'email': row[1],
                    'nickname': row[2],
                    'credentials_json': row[3],
                    'index_num': row[4],
                    'org_uuid': row[5],
                    'display_name': row[6],
                    'billing_type': row[7],
                    'org_name': row[8]
                }
                for row in rows
            ]

        if not accounts_to_refresh:
            console.print("[yellow]No accounts to refresh[/yellow]")
            return

        console.print(f"[yellow]Force refreshing {len(accounts_to_refresh)} account(s)...[/yellow]\n")

        for account in accounts_to_refresh:
            account_display = f"[{account['index_num']}] {account['nickname'] or account['email']}"

            try:
                # Fetch account info for sandbox
                account_info = {
                    "uuid": account['uuid'],
                    "email": account['email'],
                    "org_uuid": account['org_uuid'],
                    "display_name": account['display_name'],
                    "billing_type": account['billing_type'],
                    "org_name": account['org_name']
                }

                # Force refresh the token
                refreshed_creds = refresh_token(
                    account['credentials_json'],
                    account['uuid'],
                    account_info,
                    force=True
                )

                # Update database
                cursor = db.conn.cursor()
                cursor.execute(
                    "UPDATE accounts SET credentials_json = ?, updated_at = CURRENT_TIMESTAMP WHERE uuid = ?",
                    (json.dumps(refreshed_creds), account['uuid'])
                )
                db.conn.commit()

                # Check expiry
                expires_at = refreshed_creds.get("claudeAiOauth", {}).get("expiresAt", 0)
                expires_in_hours = (expires_at - int(time.time() * 1000)) / 1000 / 3600

                console.print(f"[green]✓[/green] {account_display} - expires in {expires_in_hours:.1f}h")

            except Exception as e:
                console.print(f"[red]✗[/red] {account_display} - Error: {e}")

    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        import traceback
        traceback.print_exc()
    finally:
        db.close()


@cli.command()
def cycle():
    """Cycle to the next account in the list"""
    acquire_lock()  # Lock before writing .credentials.json
    db = Database()

    try:
        accounts = db.get_all_accounts()

        if not accounts:
            console.print("[yellow]No accounts found. Add one with 'c2switcher add'[/yellow]")
            return

        if len(accounts) == 1:
            console.print("[yellow]Only one account available[/yellow]")
            return

        # Read current credentials to find current account
        # NOTE: Matching by accessToken can break after token refresh since the token changes.
        # A more robust approach would be to persist the currently selected account UUID
        # in a separate sidecar file (e.g., ~/.claude/.c2switcher-current.json).
        current_uuid = None
        if CREDENTIALS_PATH.exists():
            with open(CREDENTIALS_PATH, 'r') as f:
                try:
                    current_creds = json.load(f)
                    # Try to find UUID by comparing access tokens
                    cursor = db.conn.cursor()
                    for acc in accounts:
                        acc_creds = json.loads(acc["credentials_json"])
                        if acc_creds.get("claudeAiOauth", {}).get("accessToken") == current_creds.get("claudeAiOauth", {}).get("accessToken"):
                            current_uuid = acc["uuid"]
                            break
                except:
                    pass

        # Find next account
        if current_uuid:
            # Find current index and get next
            current_index = None
            for i, acc in enumerate(accounts):
                if acc["uuid"] == current_uuid:
                    current_index = i
                    break

            if current_index is not None:
                next_index = (current_index + 1) % len(accounts)
                next_account = accounts[next_index]
            else:
                next_account = accounts[0]
        else:
            # No current account, use first
            next_account = accounts[0]

        # Write credentials
        credentials = json.loads(next_account["credentials_json"])
        CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write_json(CREDENTIALS_PATH, credentials)

        nickname = next_account['nickname'] or '[dim]none[/dim]'
        masked_email = mask_email(next_account['email'])

        console.print(Panel(
            f"[green]Cycled to account (={next_account['index_num']})[/green]\n\n"
            f"Nickname: [bold]{nickname}[/bold]\n"
            f"Email: [bold]{masked_email}[/bold]",
            border_style="green"
        ))

    finally:
        db.close()


# Command aliases
@cli.command(name="list", hidden=True)
@click.pass_context
def list_alias(ctx):
    """List all accounts (alias for 'ls')"""
    ctx.forward(list_accounts_cmd)


@cli.command(name="list-accounts", hidden=True)
@click.pass_context
def list_accounts_alias(ctx):
    """List all accounts (alias for 'ls')"""
    ctx.forward(list_accounts_cmd)


@cli.command(name="pick", hidden=True)
@click.pass_context
def pick(ctx):
    """Find the optimal account to use (alias for 'optimal')"""
    ctx.forward(optimal)


@cli.command(name="use", hidden=True)
@click.pass_context
def use(ctx):
    """Switch to a specific account (alias for 'switch')"""
    ctx.forward(switch)


@cli.command(name="start-session")
@click.option("--session-id", required=True, help="Unique session identifier")
@click.option("--pid", required=True, type=int, help="Process ID")
@click.option("--parent-pid", type=int, help="Parent process ID")
@click.option("--cwd", required=True, help="Current working directory")
def start_session(session_id: str, pid: int, parent_pid: Optional[int], cwd: str):
    """Register a new Claude session"""
    acquire_lock()  # Lock for database write
    db = Database()

    try:
        # Get process fingerprinting data
        try:
            proc = psutil.Process(pid)
            cmdline = ' '.join(proc.cmdline())
            proc_start_time = proc.create_time()
            try:
                exe = proc.exe()
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                exe = "unknown"
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            cmdline = "unknown"
            proc_start_time = 0.0
            exe = "unknown"

        db.create_session(
            session_id=session_id,
            pid=pid,
            parent_pid=parent_pid,
            proc_start_time=proc_start_time,
            exe=exe,
            cmdline=cmdline,
            cwd=cwd
        )

        # Silently succeed - wrapper redirects stderr to /dev/null
    except Exception as e:
        # Log error but don't fail the wrapper
        console.print(f"[yellow]Warning: Failed to register session: {e}[/yellow]")
    finally:
        db.close()


@cli.command(name="end-session")
@click.option("--session-id", required=True, help="Session identifier to end")
def end_session(session_id: str):
    """Mark a Claude session as ended"""
    acquire_lock()  # Lock for database write
    db = Database()

    try:
        db.mark_session_ended(session_id)
        # Silently succeed
    except Exception as e:
        console.print(f"[yellow]Warning: Failed to end session: {e}[/yellow]")
    finally:
        db.close()


@cli.command(name="sessions")
def list_sessions():
    """List active Claude sessions"""
    db = Database()

    try:
        # Clean up dead sessions first
        cleanup_dead_sessions(db)

        active_sessions = db.get_active_sessions()

        if not active_sessions:
            console.print("[yellow]No active sessions[/yellow]")
            return

        # Build table
        table = Table(title="Active Claude Sessions", box=box.ROUNDED)
        table.add_column("Session ID", style="cyan")
        table.add_column("Account", style="green")
        table.add_column("PID", style="yellow")
        table.add_column("Working Directory", style="blue")
        table.add_column("Started", style="magenta")

        for session in active_sessions:
            # Get account email if assigned
            account_email = "not assigned"
            if session['account_uuid']:
                acc = db.get_account_by_identifier(session['account_uuid'])
                if acc:
                    account_email = acc['email']

            # Format started time
            started = session['created_at']
            started_dt = parse_sqlite_timestamp_to_local(started)

            time_ago = datetime.now() - started_dt
            if time_ago.total_seconds() < 60:
                started_str = f"{int(time_ago.total_seconds())}s ago"
            elif time_ago.total_seconds() < 3600:
                started_str = f"{int(time_ago.total_seconds() / 60)}m ago"
            else:
                started_str = f"{int(time_ago.total_seconds() / 3600)}h ago"

            # Truncate session ID
            session_id_short = session['session_id'][:8] + "..."

            # Truncate cwd
            cwd = session['cwd'] or "unknown"
            if len(cwd) > 40:
                cwd = "..." + cwd[-37:]

            table.add_row(
                session_id_short,
                account_email,
                str(session['pid']),
                cwd,
                started_str
            )

        console.print(table)
        console.print(f"\n[dim]Total active sessions: {len(active_sessions)}[/dim]")

    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
    finally:
        db.close()


@cli.command(name="session-history")
@click.option("--limit", default=20, type=int, help="Maximum number of sessions to show")
@click.option("--min-duration", default=5, type=int, help="Minimum session duration in seconds")
def session_history(limit: int, min_duration: int):
    """Show historical sessions with usage deltas"""
    db = Database()

    try:
        sessions = db.get_session_history(min_duration_seconds=min_duration, limit=limit)

        if not sessions:
            console.print(f"[yellow]No sessions found with duration >= {min_duration}s[/yellow]")
            return

        # Build table
        table = Table(title=f"Session History (duration >= {min_duration}s)", box=box.ROUNDED)
        table.add_column("Account", style="cyan")
        table.add_column("Project Path", style="blue")
        table.add_column("Duration", style="magenta", justify="right")
        table.add_column("Opus Δ", style="yellow", justify="right")
        table.add_column("Overall Δ", style="yellow", justify="right")
        table.add_column("Ended", style="dim", justify="right")

        for session in sessions:
            # Get account info
            account_display = "[dim]unknown[/dim]"
            account_uuid = session['account_uuid']

            if account_uuid:
                acc = db.get_account_by_identifier(account_uuid)
                if acc:
                    nickname = acc['nickname'] or ""
                    index = acc['index_num']
                    if nickname:
                        account_display = f"[{index}] {nickname}"
                    else:
                        account_display = f"[{index}] {acc['email']}"

            # Format project path
            cwd = session['cwd'] or "unknown"
            if len(cwd) > 45:
                cwd = "..." + cwd[-42:]

            # Format duration
            duration_seconds = session['duration_seconds']
            if duration_seconds < 60:
                duration_str = f"{int(duration_seconds)}s"
            elif duration_seconds < 3600:
                duration_str = f"{int(duration_seconds / 60)}m"
            else:
                hours = int(duration_seconds / 3600)
                minutes = int((duration_seconds % 3600) / 60)
                duration_str = f"{hours}h {minutes}m"

            # Calculate usage delta
            opus_delta = "[dim]--[/dim]"
            overall_delta = "[dim]--[/dim]"

            if account_uuid:
                # Get usage snapshots
                usage_before = db.get_usage_before(account_uuid, session['created_at'])
                usage_after = db.get_usage_after(account_uuid, session['ended_at'])

                if usage_before and usage_after:
                    before_data = usage_before['data']
                    after_data = usage_after['data']

                    # Extract Opus usage
                    before_opus = before_data.get('seven_day_opus', {})
                    after_opus = after_data.get('seven_day_opus', {})

                    before_opus_pct = before_opus.get('utilization')
                    after_opus_pct = after_opus.get('utilization')

                    if before_opus_pct is not None and after_opus_pct is not None:
                        delta = after_opus_pct - before_opus_pct
                        if delta > 0:
                            opus_delta = f"[red]+{delta}%[/red]"
                        elif delta < 0:
                            opus_delta = f"[green]{delta}%[/green]"
                        else:
                            opus_delta = "[dim]0%[/dim]"

                    # Extract overall usage
                    before_overall = before_data.get('seven_day', {})
                    after_overall = after_data.get('seven_day', {})

                    before_overall_pct = before_overall.get('utilization')
                    after_overall_pct = after_overall.get('utilization')

                    if before_overall_pct is not None and after_overall_pct is not None:
                        delta = after_overall_pct - before_overall_pct
                        if delta > 0:
                            overall_delta = f"[red]+{delta}%[/red]"
                        elif delta < 0:
                            overall_delta = f"[green]{delta}%[/green]"
                        else:
                            overall_delta = "[dim]0%[/dim]"

            # Format ended time
            ended = session['ended_at']
            ended_dt = parse_sqlite_timestamp_to_local(ended)

            time_ago = datetime.now() - ended_dt
            if time_ago.total_seconds() < 60:
                ended_str = f"{int(time_ago.total_seconds())}s ago"
            elif time_ago.total_seconds() < 3600:
                ended_str = f"{int(time_ago.total_seconds() / 60)}m ago"
            elif time_ago.total_seconds() < 86400:
                ended_str = f"{int(time_ago.total_seconds() / 3600)}h ago"
            else:
                ended_str = f"{int(time_ago.total_seconds() / 86400)}d ago"

            table.add_row(
                account_display,
                cwd,
                duration_str,
                opus_delta,
                overall_delta,
                ended_str
            )

        console.print(table)
        console.print(f"\n[dim]Total sessions: {len(sessions)}[/dim]")

    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        import traceback
        traceback.print_exc()
    finally:
        db.close()


@cli.command(name="history", hidden=True)
@click.pass_context
def history_alias(ctx):
    """Show historical sessions (alias for 'session-history')"""
    ctx.forward(session_history)


if __name__ == "__main__":
    cli()
