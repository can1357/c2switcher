"""Shared utility functions."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


def mask_email(email: str) -> str:
    """Mask email keeping first 2 and last 2 letters before @."""
    if "@" not in email:
        return email

    local, domain = email.split("@", 1)

    if len(local) <= 4:
        return f"{local[0]}***{local[-1]}@{domain}" if len(local) > 1 else f"{local}@{domain}"

    masked_local = f"{local[:2]}{'*' * (len(local) - 4)}{local[-2:]}"
    return f"{masked_local}@{domain}"


def format_time_until_reset(
    opus_resets_at: Optional[str],
    overall_resets_at: Optional[str],
    opus_usage: Optional[int] = None,
    overall_usage: Optional[int] = None,
) -> str:
    """Return human-readable window until reset plus usage rate colour-coding."""
    display_reset = opus_resets_at if opus_resets_at else overall_resets_at

    if not display_reset:
        return "[dim]--[/dim]"

    try:
        reset_dt = datetime.fromisoformat(display_reset.replace("Z", "+00:00"))
        if reset_dt.tzinfo is None:
            reset_dt = reset_dt.replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)
        time_remaining = reset_dt - now

        if time_remaining.total_seconds() <= 0:
            return "[dim]expired[/dim]"

        total_seconds = int(time_remaining.total_seconds())
        days = total_seconds // 86400
        hours = (total_seconds % 86400) // 3600
        minutes = (total_seconds % 3600) // 60

        if days > 0:
            time_str = f"{days}d{hours}h"
        elif hours > 0:
            time_str = f"{hours}h{minutes}m"
        else:
            time_str = f"{minutes}m"

        rate_str = ""
        if opus_usage is not None or overall_usage is not None:
            seven_days_seconds = 7 * 86400
            worst_rate = 0.0

            if opus_usage is not None and opus_resets_at:
                try:
                    opus_reset_dt = datetime.fromisoformat(opus_resets_at.replace("Z", "+00:00"))
                    if opus_reset_dt.tzinfo is None:
                        opus_reset_dt = opus_reset_dt.replace(tzinfo=timezone.utc)

                    opus_time_remaining = opus_reset_dt - now
                    if opus_time_remaining.total_seconds() > 0:
                        opus_elapsed = seven_days_seconds - opus_time_remaining.total_seconds()
                        opus_expected = (opus_elapsed / seven_days_seconds) * 100
                        if opus_expected > 0:
                            opus_rate = (opus_usage / opus_expected) * 100
                            worst_rate = max(worst_rate, opus_rate)
                except Exception:
                    pass

            if overall_usage is not None and overall_resets_at:
                try:
                    overall_reset_dt = datetime.fromisoformat(overall_resets_at.replace("Z", "+00:00"))
                    if overall_reset_dt.tzinfo is None:
                        overall_reset_dt = overall_reset_dt.replace(tzinfo=timezone.utc)

                    overall_time_remaining = overall_reset_dt - now
                    if overall_time_remaining.total_seconds() > 0:
                        overall_elapsed = seven_days_seconds - overall_time_remaining.total_seconds()
                        overall_expected = (overall_elapsed / seven_days_seconds) * 100
                        if overall_expected > 0:
                            overall_rate = (overall_usage / overall_expected) * 100
                            worst_rate = max(worst_rate, overall_rate)
                except Exception:
                    pass

            if worst_rate > 0:
                if worst_rate >= 120:
                    rate_str = f" [red]({worst_rate:.0f}%)[/red]"
                elif worst_rate >= 100:
                    rate_str = f" [yellow]({worst_rate:.0f}%)[/yellow]"
                else:
                    rate_str = f" [green]({worst_rate:.0f}%)[/green]"

        return time_str + rate_str

    except Exception:
        return "[dim]--[/dim]"


def atomic_write_json(path: Path, data: Dict[str, Any], preserve_permissions: bool = True):
    """Atomically write JSON to disk with optional permission preservation."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass  # Best effort

    mode = 0o600
    if preserve_permissions and path.exists():
        try:
            stat_info = path.stat()
            mode = stat_info.st_mode & 0o777
        except OSError:
            pass

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            raise

        os.replace(tmp_path, path)
        os.chmod(path, mode)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def parse_sqlite_timestamp_to_local(timestamp: Any) -> datetime:
    """Convert a SQLite timestamp to naive local datetime."""
    if isinstance(timestamp, str):
        dt_utc = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if dt_utc.tzinfo is None:
            dt_utc = dt_utc.replace(tzinfo=timezone.utc)
        return dt_utc.astimezone().replace(tzinfo=None)
    return timestamp
