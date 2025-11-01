"""Account selection logic with load balancing."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from filelock import FileLock

from .constants import (
    BURST_THRESHOLD,
    FIVE_HOUR_PENALTIES,
    FIVE_HOUR_ROTATION_CAP,
    FRESH_ACCOUNT_MAX_BONUS,
    FRESH_UTILIZATION_THRESHOLD,
    HIGH_DRAIN_REFRESH_THRESHOLD,
    LB_STATE_PATH,
    SIMILAR_DRAIN_THRESHOLD,
    STALE_CACHE_SECONDS,
    console,
)
from .database import Database
from .sessions import cleanup_dead_sessions
from .usage import get_account_usage
from .utils import atomic_write_json


def _hours_until_reset(reset_iso: Optional[str]) -> float:
    if not reset_iso:
        return 168.0  # Fall back to 7 days when reset timestamp is missing
    try:
        reset_dt = datetime.fromisoformat(reset_iso.replace("Z", "+00:00"))
        if reset_dt.tzinfo is None:
            reset_dt = reset_dt.replace(tzinfo=timezone.utc)
        hours = (reset_dt - datetime.now(timezone.utc)).total_seconds() / 3600.0
        if hours < 0:
            return 0.1
        return max(hours, 1.0 / 60.0)
    except Exception:
        return 168.0  # Fall back to 7 days on parse errors


def _load_balancer_state() -> Dict[str, Any]:
    try:
        with open(LB_STATE_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _store_balancer_state(state: Dict[str, Any]):
    try:
        atomic_write_json(LB_STATE_PATH, state)
    except Exception:
        pass


def _choose_round_robin(candidates: List[Dict]) -> Dict:
    if len(candidates) == 1:
        return candidates[0]

    min_active = min(c["active_sessions"] for c in candidates)
    candidates = [c for c in candidates if c["active_sessions"] == min_active]

    min_recent = min(c["recent_sessions"] for c in candidates)
    candidates = [c for c in candidates if c["recent_sessions"] == min_recent]

    candidates.sort(key=lambda c: c["account"]["index_num"])

    lock_path = str(LB_STATE_PATH) + ".lock"
    with FileLock(lock_path, timeout=5):
        state = _load_balancer_state()
        rr_state = state.setdefault("round_robin", {})
        window = candidates[0]["window"]
        last_uuid = rr_state.get(window)

        candidate_uuids = [c["account"]["uuid"] for c in candidates]
        if last_uuid and last_uuid not in candidate_uuids:
            last_uuid = None
            rr_state[window] = None

        next_idx = 0
        if last_uuid:
            for idx, cand in enumerate(candidates):
                if cand["account"]["uuid"] == last_uuid:
                    next_idx = (idx + 1) % len(candidates)
                    break

        selected = candidates[next_idx]
        rr_state[window] = selected["account"]["uuid"]
        _store_balancer_state(state)
    return selected


def _log_balancer_candidates(label: str, candidates: List[Dict]):
    if os.getenv("C2SWITCHER_DEBUG_BALANCER") != "1":
        return
    console.print(f"[blue]{label}[/blue]")
    for cand in candidates:
        acc = cand["account"]
        cache_age = cand.get("cache_age_seconds")
        age_str = f"{cache_age:.0f}s" if isinstance(cache_age, (int, float)) and cache_age is not None else "-"
        info = (
            f"- {acc['email']}: tier={cand['tier']} drain={cand['drain_rate']:.3f} adj={cand['adjusted_drain']:.3f} "
            f"bonus={cand['fresh_bonus']:.2f} factor={cand['five_hour_factor']:.2f} util={cand['utilization']:.1f} "
            f"headroom={cand['headroom']:.1f} burst={cand['expected_burst']:.1f} "
            f"blocked={int(cand['burst_blocked'])} hours={cand['hours_to_reset']:.1f} "
            f"five_hour={cand['five_hour_utilization']:.1f} active={cand['active_sessions']} "
            f"recent={cand['recent_sessions']} cache={cand.get('cache_source', '?')} age={age_str}"
        )
        if cand.get("refreshed"):
            info += " [green](refreshed)[/green]"
        console.print(info)


def select_account_with_load_balancing(db: Database, session_id: Optional[str] = None) -> Optional[Dict]:
    cleanup_dead_sessions(db)

    if session_id:
        existing = db.get_session_account(session_id)
        if existing:
            try:
                usage = get_account_usage(db, existing["uuid"], existing["credentials_json"])
                seven_day_opus = usage.get("seven_day_opus", {}) or {}
                seven_day = usage.get("seven_day", {}) or {}
                opus_util = seven_day_opus.get("utilization")
                overall_util = seven_day.get("utilization")
                opus_ok = opus_util is None or float(opus_util) < 99
                overall_ok = overall_util is None or float(overall_util) < 99
                if opus_ok and overall_ok:
                    return {"account": existing, "reused": True}
            except Exception:
                pass

    accounts = db.get_all_accounts()
    if not accounts:
        return None

    candidates: List[Dict[str, Any]] = []
    burst_cache: Dict[str, float] = {}

    def build_candidate(acc: Dict, usage_data: Dict, refreshed: bool = False) -> Optional[Dict[str, Any]]:
        five_hour = usage_data.get("five_hour", {}) or {}
        seven_day_opus = usage_data.get("seven_day_opus", {}) or {}
        seven_day = usage_data.get("seven_day", {}) or {}

        opus_util_raw = seven_day_opus.get("utilization")
        overall_util_raw = seven_day.get("utilization")

        opus_util = float(opus_util_raw) if opus_util_raw is not None else 100.0
        overall_util = float(overall_util_raw) if overall_util_raw is not None else 100.0

        if opus_util >= 99.0 and overall_util >= 99.0:
            return None

        if opus_util < 99.0:
            window = "opus"
            utilization = opus_util
            resets_at = seven_day_opus.get("resets_at")
            tier = 1
        else:
            window = "overall"
            utilization = overall_util
            resets_at = seven_day.get("resets_at")
            tier = 2

        hours_to_reset = _hours_until_reset(resets_at)
        headroom = max(99.0 - utilization, 0.0)
        drain_rate = headroom / max(hours_to_reset, 0.001) if headroom > 0 else 0.0

        fresh_bonus = 0.0
        if headroom > 0 and utilization < FRESH_UTILIZATION_THRESHOLD:
            freshness = (FRESH_UTILIZATION_THRESHOLD - utilization) / FRESH_UTILIZATION_THRESHOLD
            fresh_bonus = freshness * FRESH_ACCOUNT_MAX_BONUS

        priority_drain = drain_rate + fresh_bonus

        five_hour_util_raw = five_hour.get("utilization")
        five_hour_util = float(five_hour_util_raw) if five_hour_util_raw is not None else 50.0

        active_sessions = db.count_active_sessions(acc["uuid"])
        recent_sessions = db.count_recent_sessions(acc["uuid"], minutes=5)

        cache_source = usage_data.get("_cache_source", "unknown")
        cache_age = usage_data.get("_cache_age_seconds")

        expected_burst = burst_cache.get(acc["uuid"])
        if expected_burst is None or refreshed:
            expected_burst = db.get_usage_delta_percentile(acc["uuid"])
            burst_cache[acc["uuid"]] = expected_burst

        burst_blocked = (utilization + expected_burst) >= BURST_THRESHOLD

        five_hour_factor = 1.0
        for threshold, factor in FIVE_HOUR_PENALTIES:
            if five_hour_util >= threshold:
                five_hour_factor = factor
                break

        adjusted_drain = priority_drain * five_hour_factor

        rank = (
            adjusted_drain,
            utilization,
            -hours_to_reset,
            -five_hour_util,
            -active_sessions,
            -recent_sessions,
        )

        return {
            "account": acc,
            "tier": tier,
            "rank": rank,
            "drain_rate": drain_rate,
            "priority_drain": priority_drain,
            "fresh_bonus": fresh_bonus,
            "adjusted_drain": adjusted_drain,
            "utilization": utilization,
            "headroom": headroom,
            "hours_to_reset": hours_to_reset,
            "five_hour_utilization": five_hour_util,
            "five_hour_factor": five_hour_factor,
            "expected_burst": expected_burst,
            "burst_blocked": burst_blocked,
            "active_sessions": active_sessions,
            "recent_sessions": recent_sessions,
            "opus_usage": float(opus_util_raw) if opus_util_raw is not None else None,
            "overall_usage": float(overall_util_raw) if overall_util_raw is not None else None,
            "window": window,
            "cache_source": cache_source,
            "cache_age_seconds": cache_age,
            "refreshed": refreshed,
        }

    def fetch_usage(acc: Dict) -> Optional[Dict]:
        try:
            usage = get_account_usage(db, acc["uuid"], acc["credentials_json"])
            return {"account": acc, "usage": usage, "error": None}
        except Exception as exc:
            return {"account": acc, "usage": None, "error": exc}

    with ThreadPoolExecutor(max_workers=min(len(accounts), 10)) as executor:
        futures = {executor.submit(fetch_usage, acc): acc for acc in accounts}
        usage_results = []
        for future in as_completed(futures):
            result = future.result()
            usage_results.append(result)

    for result in usage_results:
        acc = result["account"]
        usage = result["usage"]
        error = result["error"]

        if error:
            console.print(f"[yellow]Warning: Could not fetch usage for {acc['email']}: {error}[/yellow]")
            continue

        candidate = build_candidate(acc, usage)
        if not candidate:
            continue

        needs_refresh = False
        cache_source = candidate.get("cache_source")
        cache_age = candidate.get("cache_age_seconds")
        if cache_source != "live":
            if cache_age is not None and cache_age > STALE_CACHE_SECONDS:
                needs_refresh = True
            if candidate["priority_drain"] >= HIGH_DRAIN_REFRESH_THRESHOLD and (cache_age is None or cache_age > 10):
                needs_refresh = True

        if needs_refresh:
            try:
                usage = get_account_usage(db, acc["uuid"], acc["credentials_json"], force=True)
                candidate = build_candidate(acc, usage, refreshed=True)
                if not candidate:
                    continue
            except Exception as exc:
                console.print(f"[yellow]Warning: Refresh failed for {acc['email']}: {exc}[/yellow]")

        candidates.append(candidate)

    if not candidates:
        return None

    usable_candidates = [c for c in candidates if not c["burst_blocked"]]
    pool = usable_candidates if usable_candidates else candidates

    cool_candidates = [c for c in pool if c["five_hour_utilization"] < FIVE_HOUR_ROTATION_CAP]
    pool = cool_candidates if cool_candidates else pool

    pool.sort(key=lambda c: c["rank"], reverse=True)

    _log_balancer_candidates("load-balancer candidates", pool)

    top = pool[0]
    similar = [
        c
        for c in pool
        if c["tier"] == top["tier"] and abs(top["adjusted_drain"] - c["adjusted_drain"]) <= SIMILAR_DRAIN_THRESHOLD
    ]

    if len(similar) > 1:
        selected = _choose_round_robin(similar)
    else:
        selected = top

    if session_id:
        db.assign_session_to_account(session_id, selected["account"]["uuid"])

    return {
        "account": selected["account"],
        "tier": selected["tier"],
        "score": selected["rank"],
        "opus_usage": selected["opus_usage"],
        "overall_usage": selected["overall_usage"],
        "headroom": selected["headroom"],
        "hours_to_reset": selected["hours_to_reset"],
        "drain_rate": selected["drain_rate"],
        "priority_drain": selected["priority_drain"],
        "fresh_bonus": selected["fresh_bonus"],
        "adjusted_drain": selected["adjusted_drain"],
        "five_hour_factor": selected["five_hour_factor"],
        "five_hour_utilization": selected["five_hour_utilization"],
        "expected_burst": selected["expected_burst"],
        "burst_blocked": selected["burst_blocked"],
        "active_sessions": selected["active_sessions"],
        "recent_sessions": selected["recent_sessions"],
        "cache_source": selected.get("cache_source"),
        "cache_age_seconds": selected.get("cache_age_seconds"),
        "refreshed": selected.get("refreshed", False),
        "reused": False,
    }
