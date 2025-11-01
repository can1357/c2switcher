"""Usage retrieval helpers."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from typing import Dict

from .api import ClaudeAPI
from .database import Database
from .tokens import refresh_token


def get_account_usage(db: Database, account_uuid: str, credentials_json: str, force: bool = False) -> Dict:
    """Fetch usage data for an account, caching recent results."""
    if not force:
        cached = db.get_recent_usage(account_uuid, max_age_seconds=300)
        if cached:
            usage, queried_at = cached
            usage_copy = copy.deepcopy(usage)
            cache_age = None
            try:
                cache_dt = datetime.fromisoformat(queried_at.replace("Z", "+00:00"))
                cache_age = max((datetime.now(timezone.utc) - cache_dt).total_seconds(), 0)
            except Exception:
                cache_age = None
            usage_copy.setdefault("_cache_source", "cache")
            usage_copy["_cache_age_seconds"] = cache_age
            usage_copy["_queried_at"] = queried_at
            return usage_copy

    refreshed_creds = refresh_token(credentials_json)
    token = refreshed_creds.get("claudeAiOauth", {}).get("accessToken")

    if not token:
        raise ValueError("No access token found in credentials")

    usage = ClaudeAPI.get_usage(token)
    usage.setdefault("_cache_source", "live")
    usage["_cache_age_seconds"] = 0.0
    usage["_queried_at"] = datetime.now(timezone.utc).isoformat()

    usage_to_store = copy.deepcopy(usage)
    usage_to_store.pop("_cache_source", None)
    usage_to_store.pop("_cache_age_seconds", None)
    usage_to_store.pop("_queried_at", None)
    db.add_usage(account_uuid, usage_to_store)

    if refreshed_creds != json.loads(credentials_json):
        cursor = db.conn.cursor()
        cursor.execute(
            "UPDATE accounts SET credentials_json = ?, updated_at = CURRENT_TIMESTAMP WHERE uuid = ?",
            (json.dumps(refreshed_creds), account_uuid),
        )
        db.conn.commit()

    return usage

