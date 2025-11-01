"""Account selection logic with load balancing."""

from __future__ import annotations

import copy
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from filelock import FileLock

from .api import ClaudeAPI
from .constants import (
    BURST_THRESHOLD,
    CACHE_TTL_SECONDS,
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
from .tokens import refresh_token
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


def _maybe_cleanup_sessions(db: Database):
    cleanup_marker = Path.home() / ".c2switcher" / ".last_cleanup"
    now = time.time()
    should_cleanup = True
    if cleanup_marker.exists():
        try:
            if now - cleanup_marker.stat().st_mtime < 30:
                should_cleanup = False
        except Exception:
            should_cleanup = True

    if should_cleanup:
        cleanup_dead_sessions(db)
        cleanup_marker.parent.mkdir(parents=True, exist_ok=True)
        cleanup_marker.touch(exist_ok=True)


def _reuse_existing_session(db: Database, session_id: str) -> Optional[Dict[str, Any]]:
    existing = db.get_session_account(session_id)
    if not existing:
        return None

    account = dict(existing)
    try:
        usage = get_account_usage(db, account["uuid"], account["credentials_json"])
        seven_day_opus = usage.get("seven_day_opus", {}) or {}
        seven_day = usage.get("seven_day", {}) or {}
        opus_util = seven_day_opus.get("utilization")
        overall_util = seven_day.get("utilization")
        opus_ok = opus_util is None or float(opus_util) < 99
        overall_ok = overall_util is None or float(overall_util) < 99
        if opus_ok and overall_ok:
            return {"account": account, "reused": True}
    except Exception:
        pass
    return None


def _collect_cached_usage(
    db: Database, accounts: List[Dict[str, Any]]
) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    cached_usage: Dict[str, Dict[str, Any]] = {}
    missing_accounts: List[Dict[str, Any]] = []

    for acc in accounts:
        cached = db.get_recent_usage(acc["uuid"], max_age_seconds=CACHE_TTL_SECONDS)
        if not cached:
            missing_accounts.append(acc)
            continue

        usage, queried_at = cached
        usage_copy = copy.deepcopy(usage)
        cache_age: Optional[float]
        try:
            cache_dt = datetime.fromisoformat(queried_at.replace("Z", "+00:00"))
            if cache_dt.tzinfo is None:
                cache_dt = cache_dt.replace(tzinfo=timezone.utc)
            cache_age = max((datetime.now(timezone.utc) - cache_dt).total_seconds(), 0)
        except Exception:
            cache_age = None

        usage_copy.setdefault("_cache_source", "cache")
        usage_copy["_cache_age_seconds"] = cache_age
        usage_copy["_queried_at"] = queried_at
        cached_usage[acc["uuid"]] = usage_copy

    return cached_usage, missing_accounts


def _fetch_usage_for_account(acc: Dict[str, Any]) -> Dict[str, Any]:
    refreshed_creds = refresh_token(acc["credentials_json"])
    token = refreshed_creds.get("claudeAiOauth", {}).get("accessToken")
    if not token:
        raise ValueError("No access token")

    usage = ClaudeAPI.get_usage(token)
    usage.setdefault("_cache_source", "live")
    usage["_cache_age_seconds"] = 0.0
    usage["_queried_at"] = datetime.now(timezone.utc).isoformat()
    return {"usage": usage, "refreshed_creds": refreshed_creds}


def _fetch_usage_for_accounts(accounts: List[Dict[str, Any]], label: str) -> Dict[str, Dict[str, Any]]:
    if not accounts:
        return {}

    results: Dict[str, Dict[str, Any]] = {}
    max_workers = min(len(accounts), 10)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(_fetch_usage_for_account, acc): acc for acc in accounts}
        for future in as_completed(future_map):
            acc = future_map[future]
            try:
                payload = future.result()
            except Exception as exc:
                console.print(
                    f"[yellow]Warning: Could not fetch usage for {acc['email']} ({label}): {exc}[/yellow]"
                )
                continue
            results[acc["uuid"]] = payload

    return results


def _persist_usage_results(
    db: Database, account_lookup: Dict[str, Dict[str, Any]], fetched_results: Dict[str, Dict[str, Any]]
):
    if not fetched_results:
        return

    cursor = db.conn.cursor()
    for account_uuid, payload in fetched_results.items():
        usage = payload.get("usage")
        if not usage:
            continue

        usage_to_store = copy.deepcopy(usage)
        usage_to_store.pop("_cache_source", None)
        usage_to_store.pop("_cache_age_seconds", None)
        usage_to_store.pop("_queried_at", None)
        db.add_usage(account_uuid, usage_to_store)

        refreshed_creds = payload.get("refreshed_creds")
        if not refreshed_creds:
            continue

        account = account_lookup.get(account_uuid)
        current_serialized = account.get("credentials_json") if account else None

        current_creds: Optional[Dict[str, Any]] = None
        if current_serialized:
            try:
                current_creds = json.loads(current_serialized)
            except json.JSONDecodeError:
                current_creds = None

        if current_creds == refreshed_creds:
            continue

        serialized = json.dumps(refreshed_creds)
        if account is not None:
            account["credentials_json"] = serialized

        cursor.execute(
            "UPDATE accounts SET credentials_json = ?, updated_at = CURRENT_TIMESTAMP WHERE uuid = ?",
            (serialized, account_uuid),
        )
    db.conn.commit()


def _get_recent_session_counts(db: Database, minutes: int = 5) -> Dict[str, int]:
    cursor = db.conn.cursor()
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


def _build_candidate(
    db: Database,
    acc: Dict[str, Any],
    usage_data: Dict[str, Any],
    burst_cache: Dict[str, float],
    active_counts: Dict[str, int],
    recent_counts: Dict[str, int],
    *,
    refreshed: bool,
) -> Optional[Dict[str, Any]]:
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

    account_uuid = acc["uuid"]
    active_sessions = active_counts.get(account_uuid, 0)
    recent_sessions = recent_counts.get(account_uuid, 0)

    expected_burst = burst_cache.get(account_uuid)
    if expected_burst is None or refreshed:
        expected_burst = db.get_usage_delta_percentile(account_uuid)
        burst_cache[account_uuid] = expected_burst

    burst_blocked = (utilization + expected_burst) >= BURST_THRESHOLD

    five_hour_factor = 1.0
    for threshold, factor in FIVE_HOUR_PENALTIES:
        if five_hour_util >= threshold:
            five_hour_factor = factor
            break

    adjusted_drain = priority_drain * five_hour_factor
    cache_source = usage_data.get("_cache_source", "unknown")
    cache_age = usage_data.get("_cache_age_seconds")

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


def _candidate_needs_refresh(candidate: Dict[str, Any]) -> bool:
    if candidate.get("refreshed"):
        return False

    cache_source = candidate.get("cache_source")
    cache_age = candidate.get("cache_age_seconds")
    if cache_source == "live":
        return False

    if cache_age is not None and cache_age > STALE_CACHE_SECONDS:
        return True

    if candidate["priority_drain"] >= HIGH_DRAIN_REFRESH_THRESHOLD and (cache_age is None or cache_age > 10):
        return True

    return False


def _build_candidates(
    db: Database,
    accounts: List[Dict[str, Any]],
    usage_by_uuid: Dict[str, Dict[str, Any]],
    active_counts: Dict[str, int],
    recent_counts: Dict[str, int],
    burst_cache: Dict[str, float],
    refreshed_ids: Set[str],
    *,
    allow_refresh: bool,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    candidates: List[Dict[str, Any]] = []
    refresh_accounts: List[Dict[str, Any]] = []

    for acc in accounts:
        usage = usage_by_uuid.get(acc["uuid"])
        if not usage:
            continue

        candidate = _build_candidate(
            db,
            acc,
            usage,
            burst_cache,
            active_counts,
            recent_counts,
            refreshed=acc["uuid"] in refreshed_ids,
        )

        if not candidate:
            continue

        candidates.append(candidate)

        if allow_refresh and _candidate_needs_refresh(candidate):
            refresh_accounts.append(acc)

    return candidates, refresh_accounts


def _select_candidate(candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
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
        return _choose_round_robin(similar)

    return top


def select_account_with_load_balancing(db: Database, session_id: Optional[str] = None) -> Optional[Dict]:
    _maybe_cleanup_sessions(db)

    if session_id:
        reused = _reuse_existing_session(db, session_id)
        if reused:
            return reused

    raw_accounts = db.get_all_accounts()
    if not raw_accounts:
        return None

    accounts = [dict(acc) for acc in raw_accounts]
    account_lookup = {acc["uuid"]: acc for acc in accounts}

    usage_by_uuid, accounts_needing_fetch = _collect_cached_usage(db, accounts)

    initial_fetch_results = _fetch_usage_for_accounts(accounts_needing_fetch, label="initial fetch")
    if initial_fetch_results:
        _persist_usage_results(db, account_lookup, initial_fetch_results)
        for account_uuid, payload in initial_fetch_results.items():
            usage_by_uuid[account_uuid] = payload["usage"]

    if not usage_by_uuid:
        return None

    active_counts = db.get_all_active_session_counts()
    recent_counts = _get_recent_session_counts(db, minutes=5)
    burst_cache: Dict[str, float] = {}
    refreshed_ids: Set[str] = set()

    candidates, refresh_accounts = _build_candidates(
        db,
        accounts,
        usage_by_uuid,
        active_counts,
        recent_counts,
        burst_cache,
        refreshed_ids,
        allow_refresh=True,
    )

    if refresh_accounts:
        refresh_results = _fetch_usage_for_accounts(refresh_accounts, label="refresh")
        if refresh_results:
            _persist_usage_results(db, account_lookup, refresh_results)
            for account_uuid, payload in refresh_results.items():
                usage_by_uuid[account_uuid] = payload["usage"]
                refreshed_ids.add(account_uuid)

            candidates, _ = _build_candidates(
                db,
                accounts,
                usage_by_uuid,
                active_counts,
                recent_counts,
                burst_cache,
                refreshed_ids,
                allow_refresh=False,
            )

    if not candidates:
        return None

    selected = _select_candidate(candidates)
    if not selected:
        return None

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
