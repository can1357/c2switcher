"""Session tracking helpers."""

from __future__ import annotations

import os
from typing import Dict, Optional

import psutil

from .constants import console
from .database import Database


def is_session_alive(session: Dict) -> bool:
    """Run multi-factor liveness checks against stored process fingerprints."""
    debug = os.environ.get("DEBUG_SESSIONS") == "1"

    try:
        proc = psutil.Process(session["pid"])

        if not proc.is_running():
            if debug:
                print(f"[DEBUG] PID {session['pid']}: not running")
            return False

        if session.get("proc_start_time"):
            proc_start_time = proc.create_time()
            stored_start_time = session["proc_start_time"]
            if abs(proc_start_time - stored_start_time) >= 1.0:
                if debug:
                    print(
                        f"[DEBUG] PID {session['pid']}: start time mismatch "
                        f"(proc={proc_start_time}, stored={stored_start_time})"
                    )
                return False

        if session.get("exe"):
            try:
                proc_exe = proc.exe()
                if proc_exe != session["exe"]:
                    if debug:
                        print(
                            f"[DEBUG] PID {session['pid']}: exe mismatch "
                            f"(proc={proc_exe}, stored={session['exe']})"
                        )
                    return False
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass

        if debug:
            print(f"[DEBUG] PID {session['pid']}: ALIVE ✓")
        return True

    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired, ValueError) as exc:
        if debug:
            print(f"[DEBUG] PID {session['pid']}: exception {exc}")
        return False


def cleanup_dead_sessions(db: Database):
    """Mark dead sessions as ended and update last_checked for live ones."""
    active_sessions = db.get_active_sessions()

    for session in active_sessions:
        if is_session_alive(dict(session)):
            db.update_session_last_checked(session["session_id"])
        else:
            db.mark_session_ended(session["session_id"])


def register_session(db: Database, session_id: str, pid: int, parent_pid: Optional[int], cwd: str):
    """Record session metadata in the database, fetching process fingerprints."""
    try:
        proc = psutil.Process(pid)
        cmdline = " ".join(proc.cmdline())
        proc_start_time = proc.create_time()
        try:
            exe = proc.exe()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            exe = "unknown"
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        cmdline = "unknown"
        proc_start_time = 0.0
        exe = "unknown"

    try:
        db.create_session(
            session_id=session_id,
            pid=pid,
            parent_pid=parent_pid,
            proc_start_time=proc_start_time,
            exe=exe,
            cmdline=cmdline,
            cwd=cwd,
        )
    except Exception as exc:
        console.print(f"[yellow]Warning: Failed to register session: {exc}[/yellow]")

