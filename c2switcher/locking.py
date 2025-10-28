"""Filesystem locking helpers."""

from __future__ import annotations

import atexit
import contextlib
import os
import sys
import time
from pathlib import Path
from typing import Optional

from filelock import FileLock as FileLocker, Timeout as FileLockTimeout

from .constants import LOCK_PATH, console

_lock_acquired: Optional["FileLock"] = None


class FileLock:
    """Cross-platform file-based locking mechanism to prevent concurrent writes."""

    def __init__(self, lock_path: Path = LOCK_PATH):
        self.lock_path = lock_path
        self.pid_path = lock_path.with_suffix(".pid")
        self.lock = FileLocker(str(lock_path), timeout=-1)
        self.acquired = False

    def acquire(self, timeout: int = 30, max_retries: int = 300):
        """Acquire exclusive lock, waiting up to timeout seconds with retries."""
        start_time = time.time()
        shown_waiting_msg = False
        retries = 0

        self.lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self.lock_path.parent, 0o700)
        except OSError:
            pass

        while retries < max_retries:
            try:
                self.lock.acquire(timeout=0.001)
                self.acquired = True

                try:
                    fd = os.open(self.pid_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "w") as handle:
                        handle.write(f"{os.getpid()}\n")
                        handle.flush()
                        os.fsync(handle.fileno())
                except OSError:
                    pass

                if shown_waiting_msg:
                    console.print("[green]✓ Lock acquired[/green]")
                return

            except FileLockTimeout:
                retries += 1
                elapsed = time.time() - start_time

                if elapsed >= timeout:
                    pid_info = self._read_pid()
                    if pid_info:
                        console.print(
                            f"[red]Error: Timeout waiting for c2switcher operation (PID: {pid_info}) to complete[/red]"
                        )
                    else:
                        console.print("[red]Error: Timeout waiting for c2switcher operation to complete[/red]")
                    sys.exit(1)

                if not shown_waiting_msg:
                    pid_info = self._read_pid()
                    if pid_info:
                        console.print(
                            f"[yellow]Waiting for another c2switcher operation to complete (PID: {pid_info})...[/yellow]"
                        )
                    else:
                        console.print("[yellow]Waiting for another c2switcher operation to complete...[/yellow]")
                    shown_waiting_msg = True

                time.sleep(0.1)

            except Exception as exc:
                console.print(f"[red]Error acquiring lock: {exc}[/red]")
                sys.exit(1)

        console.print(f"[red]Error: Maximum retries ({max_retries}) exceeded waiting for lock[/red]")
        sys.exit(1)

    def _read_pid(self) -> Optional[str]:
        """Read PID from lock file for debugging."""
        try:
            if self.pid_path.exists():
                with open(self.pid_path, "r") as handle:
                    return handle.read().strip()
        except Exception:
            pass
        return None

    def release(self):
        """Release the lock."""
        if self.acquired:
            try:
                self.lock.release()
                self.acquired = False
                with contextlib.suppress(FileNotFoundError, OSError):
                    self.pid_path.unlink()
            except Exception:
                pass


def _release_lock():
    global _lock_acquired
    if _lock_acquired is not None:
        _lock_acquired.release()
        _lock_acquired = None


def acquire_lock():
    """Acquire an exclusive process-wide lock (idempotent)."""
    global _lock_acquired
    if _lock_acquired is not None:
        return

    lock = FileLock()
    lock.acquire()
    _lock_acquired = lock
    atexit.register(_release_lock)

