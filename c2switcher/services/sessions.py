"""Session lifecycle management."""

from __future__ import annotations

import os
import time
from typing import List, Optional

import psutil

from ..constants import C2SWITCHER_DIR
from ..core.errors import SessionRegistrationError
from ..core.models import Account, Session
from ..data.store import Store


class SessionService:
    """
    Manages session registration and liveness tracking.

    Responsibilities:
    - Register new Claude Code sessions
    - Check session liveness via psutil
    - Periodic cleanup of dead sessions
    """

    def __init__(self, store: Store):
        self.store = store
        self.cleanup_marker = C2SWITCHER_DIR / '.last_cleanup'

    def register(self, session_id: str, pid: int, parent_pid: Optional[int], cwd: str) -> Session:
        """
        Register new session with process metadata.

        Args:
           session_id: Unique session identifier
           pid: Process ID
           parent_pid: Parent process ID
           cwd: Current working directory

        Returns:
           Created Session

        Raises:
           SessionRegistrationError: If registration fails
        """
        try:
            proc = psutil.Process(pid)
            cmdline = ' '.join(proc.cmdline())
            proc_start_time = proc.create_time()
            try:
                exe = proc.exe()
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                exe = 'unknown'
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            cmdline = 'unknown'
            proc_start_time = 0.0
            exe = 'unknown'

        try:
            return self.store.create_session(
                session_id=session_id,
                pid=pid,
                parent_pid=parent_pid,
                proc_start_time=proc_start_time,
                exe=exe,
                cmdline=cmdline,
                cwd=cwd,
            )
        except Exception as exc:
            raise SessionRegistrationError(f'Failed to register session: {exc}')

    def is_alive(self, session: Session) -> bool:
        """
        Multi-factor liveness check.

        Validates:
        - PID exists
        - Process start time matches
        - Executable path matches
        """
        debug = os.environ.get('DEBUG_SESSIONS') == '1'

        try:
            proc = psutil.Process(session.pid)

            if not proc.is_running():
                if debug:
                    print(f'[DEBUG] PID {session.pid}: not running')
                return False

            if session.proc_start_time:
                proc_start_time = proc.create_time()
                if abs(proc_start_time - session.proc_start_time) >= 1.0:
                    if debug:
                        print(
                            f'[DEBUG] PID {session.pid}: start time mismatch '
                            f'(proc={proc_start_time}, stored={session.proc_start_time})'
                        )
                    return False

            if session.exe:
                try:
                    proc_exe = proc.exe()
                    if proc_exe != session.exe:
                        if debug:
                            print(f'[DEBUG] PID {session.pid}: exe mismatch (proc={proc_exe}, stored={session.exe})')
                        return False
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    pass

            if debug:
                print(f'[DEBUG] PID {session.pid}: ALIVE ✓')
            return True

        except (
            psutil.NoSuchProcess,
            psutil.AccessDenied,
            psutil.TimeoutExpired,
            ValueError,
        ) as exc:
            if debug:
                print(f'[DEBUG] PID {session.pid}: exception {exc}')
            return False

    def cleanup_dead_sessions(self) -> int:
        """
        Mark dead sessions as ended.

        Returns:
           Number of sessions ended
        """
        active_sessions = self.store.list_active_sessions()
        ended_count = 0

        for session in active_sessions:
            if self.is_alive(session):
                self.store.update_session_last_checked(session.session_id)
            else:
                self.store.mark_session_ended(session.session_id)
                ended_count += 1

        return ended_count

    def maybe_cleanup(self, interval_seconds: int = 30):
        """
        Run cleanup if enough time has passed.

        Args:
           interval_seconds: Minimum seconds between cleanups
        """
        now = time.time()
        should_cleanup = True

        if self.cleanup_marker.exists():
            try:
                if now - self.cleanup_marker.stat().st_mtime < interval_seconds:
                    should_cleanup = False
            except Exception:
                should_cleanup = True

        if should_cleanup:
            self.cleanup_dead_sessions()
            self.cleanup_marker.parent.mkdir(parents=True, exist_ok=True)
            self.cleanup_marker.touch(exist_ok=True)

    def list_active(self) -> List[Session]:
        """List all active sessions."""
        return self.store.list_active_sessions()

    def get_session_account(self, session_id: str) -> Optional[tuple[Session, Account]]:
        """
        Retrieve session with its assigned account.

        Returns:
           (Session, Account) tuple or None
        """
        return self.store.get_session_account(session_id)
