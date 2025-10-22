#!/usr/bin/env python3
"""
Claude Code Account Switcher - Manage multiple Claude Code accounts
"""

import os
import json
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List
import requests
import psutil
import click
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

console = Console()

# Paths
DB_PATH = Path.home() / ".c2switcher.db"
CLAUDE_DIR = Path.home() / ".claude"
CREDENTIALS_PATH = CLAUDE_DIR / ".credentials.json"


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


def is_session_alive(session: Dict) -> bool:
    """
    Multi-factor liveness check to determine if a session is still active.
    Prevents false positives from PID reuse.
    """
    import os
    debug = os.environ.get('DEBUG_SESSIONS') == '1'

    try:
        proc = psutil.Process(session['pid'])

        # Check 1: Process is running
        if not proc.is_running():
            if debug: print(f"[DEBUG] PID {session['pid']}: not running")
            return False

        # Check 2: Command line contains 'claude'
        cmdline = ' '.join(proc.cmdline()).lower()
        if 'claude' not in cmdline:
            if debug: print(f"[DEBUG] PID {session['pid']}: cmdline '{cmdline}' doesn't contain 'claude'")
            return False

        # Check 3: Process start time should be before session creation
        # This prevents PID reuse false positives
        proc_start = datetime.fromtimestamp(proc.create_time())

        # Parse session created_at (could be datetime or string)
        # SQLite CURRENT_TIMESTAMP returns UTC, need to convert to local time
        if isinstance(session['created_at'], str):
            # Parse as UTC and convert to local time
            from datetime import timezone
            session_start_utc = datetime.fromisoformat(session['created_at'].replace('Z', '+00:00'))
            if session_start_utc.tzinfo is None:
                # Naive datetime from SQLite - treat as UTC
                session_start_utc = session_start_utc.replace(tzinfo=timezone.utc)
            session_start = session_start_utc.astimezone().replace(tzinfo=None)
        else:
            session_start = session['created_at']

        # Process must have started before session was created (or shortly after due to clock skew)
        # Allow up to 30 seconds for wrapper startup time before session registration
        time_diff = (session_start - proc_start).total_seconds()
        if debug:
            print(f"[DEBUG] PID {session['pid']}: proc_start={proc_start}, session_start={session_start}, diff={time_diff}s")

        if time_diff < -2:  # Session created >2s before process started (impossible/clock skew)
            if debug: print(f"[DEBUG] PID {session['pid']}: time_diff {time_diff} < -2 (session created before process)")
            return False
        if time_diff > 30:  # Session created >30s after process started (likely PID reuse)
            if debug: print(f"[DEBUG] PID {session['pid']}: time_diff {time_diff} > 30 (too old)")
            return False

        if debug: print(f"[DEBUG] PID {session['pid']}: ALIVE ✓")
        return True

    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired, ValueError) as e:
        if debug: print(f"[DEBUG] PID {session['pid']}: exception {e}")
        return False


def cleanup_dead_sessions(db: 'Database'):
    """
    Mark dead sessions as ended.
    Called before any session-aware command to maintain accurate state.
    """
    active_sessions = db.get_active_sessions()

    for session in active_sessions:
        if not is_session_alive(dict(session)):
            db.mark_session_ended(session['session_id'])


class Database:
    """SQLite database manager for account and usage data"""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.conn = None
        self.init_db()

    def init_db(self):
        """Initialize database schema"""
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row

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

        # Index for faster queries
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_usage_account_time
            ON usage_history(account_uuid, queried_at DESC)
        """)

        # Sessions table for tracking active Claude instances
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                account_uuid TEXT,
                pid INTEGER NOT NULL,
                parent_pid INTEGER,
                cmdline TEXT,
                cwd TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_checked_alive TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ended_at TIMESTAMP,
                FOREIGN KEY (account_uuid) REFERENCES accounts(uuid)
            )
        """)

        # Indexes for session queries
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_sessions_active
            ON sessions(ended_at) WHERE ended_at IS NULL
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
            self.conn.commit()
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
            self.conn.commit()
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
                      cmdline: str, cwd: str):
        """Create a new session record"""
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO sessions (session_id, pid, parent_pid, cmdline, cwd)
            VALUES (?, ?, ?, ?, ?)
        """, (session_id, pid, parent_pid, cmdline, cwd))
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


class ClaudeAPI:
    """Claude API client for profile and usage endpoints"""

    BASE_URL = "https://api.anthropic.com/api/oauth"

    @staticmethod
    def _get_headers(token: str) -> Dict[str, str]:
        """Get common API headers with authorization token"""
        return {
            "accept": "application/json, text/plain, */*",
            "accept-encoding": "gzip, compress, deflate, br",
            "anthropic-beta": "oauth-2025-04-20",
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
            "user-agent": "claude-code/2.0.20",
            "connection": "keep-alive",
            "host": "api.anthropic.com",
        }

    @staticmethod
    def get_profile(token: str) -> Dict:
        """Get profile information"""
        response = requests.get(
            f"{ClaudeAPI.BASE_URL}/profile",
            headers=ClaudeAPI._get_headers(token)
        )
        response.raise_for_status()
        return response.json()

    @staticmethod
    def get_usage(token: str) -> Dict:
        """Get usage information"""
        response = requests.get(
            f"{ClaudeAPI.BASE_URL}/usage",
            headers=ClaudeAPI._get_headers(token)
        )
        response.raise_for_status()
        return response.json()


def refresh_token_via_claude(credentials_json: str) -> Dict:
    """Refresh token by writing credentials and running claude code"""
    # Parse credentials
    creds = json.loads(credentials_json)

    # Check if token is expired
    expires_at = creds.get("claudeAiOauth", {}).get("expiresAt", 0)
    if expires_at > int(time.time() * 1000):
        # Token not expired
        return creds

    # Write credentials to file
    CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
    with open(CREDENTIALS_PATH, 'w') as f:
        json.dump(creds, f)

    console.print("[yellow]Token expired, refreshing via Claude Code...[/yellow]")

    # Try /status command first (doesn't consume usage)
    try:
        result = subprocess.run(
            ["claude", "-p", "/status", "--verbose", "--output-format=json"],
            timeout=30,
            capture_output=True,
            check=False
        )

        # Read credentials to check if token was refreshed
        with open(CREDENTIALS_PATH, 'r') as f:
            refreshed_creds = json.load(f)

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
            check=False
        )
    except subprocess.TimeoutExpired:
        pass
    except FileNotFoundError:
        console.print("[red]Error: 'claude' command not found. Please ensure Claude Code is installed.[/red]")
        raise

    # Read refreshed credentials
    with open(CREDENTIALS_PATH, 'r') as f:
        refreshed_creds = json.load(f)

    return refreshed_creds


def get_account_usage(db: Database, account_uuid: str, credentials_json: str, force: bool = False) -> Dict:
    """Get usage for an account, using cache if available"""
    # Check cache first
    if not force:
        cached = db.get_recent_usage(account_uuid, max_age_seconds=30)
        if cached:
            return cached

    # Refresh token if needed
    refreshed_creds = refresh_token_via_claude(credentials_json)
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

            # Session penalties:
            # - 15 points per active session (currently running)
            # - 5 points per recent session (ran in last 5min, for rate limiting)
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

        # Get access token
        token = credentials.get("claudeAiOauth", {}).get("accessToken")
        if not token:
            console.print("[red]Error: No access token found in credentials[/red]")
            return

        # Refresh if needed
        credentials = refresh_token_via_claude(json.dumps(credentials))
        token = credentials.get("claudeAiOauth", {}).get("accessToken")

        # Get profile
        with console.status("[bold green]Fetching account profile..."):
            profile = ClaudeAPI.get_profile(token)

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
            console.print(json.dumps(result, indent=2))
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
            console.print(json.dumps(result, indent=2))
        else:
            table = Table(title="Usage Across Accounts", box=box.ROUNDED)
            table.add_column("Index", style="cyan", justify="center")
            table.add_column("Nickname", style="magenta")
            table.add_column("Email", style="green")
            table.add_column("5h", justify="right")
            table.add_column("7d", justify="right")
            table.add_column("7d Opus", justify="right")
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

                    table.add_row(
                        str(acc["index_num"]),
                        acc["nickname"] or "[dim]--[/dim]",
                        acc["email"],
                        format_usage(five_hour.get("utilization")),
                        format_usage(seven_day.get("utilization")),
                        format_usage(seven_day_opus.get("utilization")),
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
                    if isinstance(started, str):
                        # SQLite CURRENT_TIMESTAMP is UTC - convert to local time
                        from datetime import timezone
                        started_dt_utc = datetime.fromisoformat(started.replace('Z', '+00:00'))
                        if started_dt_utc.tzinfo is None:
                            started_dt_utc = started_dt_utc.replace(tzinfo=timezone.utc)
                        started_dt = started_dt_utc.astimezone().replace(tzinfo=None)
                    else:
                        started_dt = started

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
def optimal(switch: bool, session_id: Optional[str]):
    """Find the optimal account with load balancing and session stickiness"""
    db = Database()

    try:
        # Use new load balancing function
        result = select_account_with_load_balancing(db, session_id)

        if not result:
            console.print("[red]No accounts available (all at capacity or no accounts found)[/red]")
            return

        optimal_acc = result['account']
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

        console.print(Panel(
            info_text,
            border_style="green"
        ))

        if switch:
            # Write credentials
            credentials = json.loads(optimal_acc["credentials_json"])
            CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
            with open(CREDENTIALS_PATH, 'w') as f:
                json.dump(credentials, f, indent=2)

            # Optionally display success message
            if not session_id:
                console.print("[green]✓[/green] Switched to optimal account")

    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
    finally:
        db.close()


@cli.command()
@click.argument("identifier")
def switch(identifier: str):
    """Switch to a specific account by index, nickname, email, or UUID"""
    db = Database()

    try:
        account = db.get_account_by_identifier(identifier)

        if not account:
            console.print(f"[red]Account not found: {identifier}[/red]")
            return

        # Write credentials
        credentials = json.loads(account["credentials_json"])
        CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
        with open(CREDENTIALS_PATH, 'w') as f:
            json.dump(credentials, f, indent=2)

        nickname = account['nickname'] or '[dim]none[/dim]'
        masked_email = mask_email(account['email'])

        console.print(Panel(
            f"[green]Switched to account (={account['index_num']})[/green]\n\n"
            f"Nickname: [bold]{nickname}[/bold]\n"
            f"Email: [bold]{masked_email}[/bold]",
            border_style="green"
        ))

    finally:
        db.close()


@cli.command()
def cycle():
    """Cycle to the next account in the list"""
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
        current_uuid = None
        if CREDENTIALS_PATH.exists():
            with open(CREDENTIALS_PATH, 'r') as f:
                try:
                    current_creds = json.load(f)
                    # Try to find UUID in credentials
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
        with open(CREDENTIALS_PATH, 'w') as f:
            json.dump(credentials, f, indent=2)

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
@cli.command(name="list")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
def list_alias(output_json: bool):
    """List all accounts (alias for 'ls')"""
    from click import Context
    ctx = Context(list_accounts_cmd)
    ctx.invoke(list_accounts_cmd, output_json=output_json)


@cli.command(name="list-accounts")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
def list_accounts_alias(output_json: bool):
    """List all accounts (alias for 'ls')"""
    from click import Context
    ctx = Context(list_accounts_cmd)
    ctx.invoke(list_accounts_cmd, output_json=output_json)


@cli.command(name="pick")
@click.option("--switch", is_flag=True, help="Actually switch to the optimal account")
def pick(switch: bool):
    """Find the optimal account to use (alias for 'optimal')"""
    from click import Context
    ctx = Context(optimal)
    ctx.invoke(optimal, switch=switch)


@cli.command(name="use")
@click.argument("identifier")
def use(identifier: str):
    """Switch to a specific account (alias for 'switch')"""
    from click import Context
    ctx = Context(switch)
    ctx.invoke(switch, identifier=identifier)


@cli.command(name="start-session")
@click.option("--session-id", required=True, help="Unique session identifier")
@click.option("--pid", required=True, type=int, help="Process ID")
@click.option("--parent-pid", type=int, help="Parent process ID")
@click.option("--cwd", required=True, help="Current working directory")
def start_session(session_id: str, pid: int, parent_pid: Optional[int], cwd: str):
    """Register a new Claude session"""
    db = Database()

    try:
        # Get command line for the process
        try:
            proc = psutil.Process(pid)
            cmdline = ' '.join(proc.cmdline())
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            cmdline = "unknown"

        db.create_session(
            session_id=session_id,
            pid=pid,
            parent_pid=parent_pid,
            cmdline=cmdline,
            cwd=cwd
        )

        # Silently succeed - wrapper redirects stderr to /dev/null
    except Exception as e:
        # Log error but don't fail the wrapper
        console.print(f"[yellow]Warning: Failed to register session: {e}[/yellow]", err=True)
    finally:
        db.close()


@cli.command(name="end-session")
@click.option("--session-id", required=True, help="Session identifier to end")
def end_session(session_id: str):
    """Mark a Claude session as ended"""
    db = Database()

    try:
        db.mark_session_ended(session_id)
        # Silently succeed
    except Exception as e:
        console.print(f"[yellow]Warning: Failed to end session: {e}[/yellow]", err=True)
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
            if isinstance(started, str):
                # SQLite CURRENT_TIMESTAMP is UTC - convert to local time
                from datetime import timezone
                started_dt_utc = datetime.fromisoformat(started.replace('Z', '+00:00'))
                if started_dt_utc.tzinfo is None:
                    started_dt_utc = started_dt_utc.replace(tzinfo=timezone.utc)
                started_dt = started_dt_utc.astimezone().replace(tzinfo=None)
            else:
                started_dt = started

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
            if isinstance(ended, str):
                # SQLite CURRENT_TIMESTAMP is UTC - convert to local time
                from datetime import timezone
                ended_dt_utc = datetime.fromisoformat(ended.replace('Z', '+00:00'))
                if ended_dt_utc.tzinfo is None:
                    ended_dt_utc = ended_dt_utc.replace(tzinfo=timezone.utc)
                ended_dt = ended_dt_utc.astimezone().replace(tzinfo=None)
            else:
                ended_dt = ended

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


@cli.command(name="history")
@click.option("--limit", default=20, type=int, help="Maximum number of sessions to show")
@click.option("--min-duration", default=5, type=int, help="Minimum session duration in seconds")
def history_alias(limit: int, min_duration: int):
    """Show historical sessions (alias for 'session-history')"""
    from click import Context
    ctx = Context(session_history)
    ctx.invoke(session_history, limit=limit, min_duration=min_duration)


if __name__ == "__main__":
    cli()
