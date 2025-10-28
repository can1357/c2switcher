"""HTTP client for Claude APIs."""

from __future__ import annotations

from typing import Dict

import requests

from .config import load_headers_config


class ClaudeAPI:
    """Claude API client for profile and usage endpoints."""

    BASE_URL = "https://api.anthropic.com/api/oauth"
    TIMEOUT = (5, 20)

    @staticmethod
    def _get_headers(token: str) -> Dict[str, str]:
        headers = load_headers_config()
        headers["authorization"] = f"Bearer {token}"
        return headers

    @staticmethod
    def get_profile(token: str) -> Dict:
        response = requests.get(
            f"{ClaudeAPI.BASE_URL}/profile",
            headers=ClaudeAPI._get_headers(token),
            timeout=ClaudeAPI.TIMEOUT,
        )
        response.raise_for_status()
        return response.json()

    @staticmethod
    def get_usage(token: str) -> Dict:
        response = requests.get(
            f"{ClaudeAPI.BASE_URL}/usage",
            headers=ClaudeAPI._get_headers(token),
            timeout=ClaudeAPI.TIMEOUT,
        )
        response.raise_for_status()
        return response.json()

