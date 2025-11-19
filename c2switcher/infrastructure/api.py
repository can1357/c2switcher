"""HTTP client for Claude APIs."""

from __future__ import annotations

import time
from typing import Dict

import requests

from ..config import load_headers_config


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
    def get_usage(token: str, max_retries: int = 3) -> Dict:
        """
        Get usage data with retry logic for intermittent null responses.

        The Anthropic API sometimes returns all null fields intermittently.
        This retries up to max_retries times if all usage fields are null.
        """
        last_response = None

        for attempt in range(max_retries):
            response = requests.get(
                f"{ClaudeAPI.BASE_URL}/usage",
                headers=ClaudeAPI._get_headers(token),
                timeout=ClaudeAPI.TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()

            # Check if we got actual usage data (not all null)
            has_data = any([
                data.get('five_hour'),
                data.get('seven_day'),
                data.get('seven_day_opus')
            ])

            if has_data:
                return data

            last_response = data

            # Wait before retry (exponential backoff: 0.5s, 1s)
            if attempt < max_retries - 1:
                time.sleep(0.5 * (2 ** attempt))

        # Return last response even if all null (caller will handle fallback)
        return last_response

