"""Configuration helpers for headers and defaults."""

from __future__ import annotations

import json
from typing import Dict

from .constants import HEADERS_PATH
from .utils import atomic_write_json


def load_headers_config() -> Dict[str, str]:
    """Load headers configuration, creating defaults if missing."""
    default_headers = {
        'accept': 'application/json, text/plain, */*',
        'accept-encoding': 'gzip, compress, deflate, br',
        'anthropic-beta': 'oauth-2025-04-20',
        'content-type': 'application/json',
        'user-agent': 'claude-code/2.0.20',
        'connection': 'keep-alive',
    }

    HEADERS_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    if not HEADERS_PATH.exists():
        try:
            atomic_write_json(HEADERS_PATH, default_headers, preserve_permissions=False)
        except Exception:
            return default_headers

    try:
        with open(HEADERS_PATH, 'r', encoding='utf-8') as handle:
            config = json.load(handle)
            headers = default_headers.copy()
            headers.update(config)
            return headers
    except Exception:
        return default_headers
