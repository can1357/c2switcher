"""Token refresh helpers."""

from __future__ import annotations

import copy
import json
import time
from typing import Dict, Optional

import requests

from .constants import console


def refresh_token_direct(credentials_json: str) -> Optional[Dict]:
    """Attempt to refresh the access token via Anthropic's OAuth endpoint."""
    creds = json.loads(credentials_json)
    oauth = creds.get("claudeAiOauth", {})
    refresh_token = oauth.get("refreshToken")

    if not refresh_token:
        return None

    try:
        response = requests.post(
            "https://console.anthropic.com/v1/oauth/token",
            json={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
            },
            timeout=10,
        )

        if response.status_code == 200:
            token_data = response.json()
            new_creds = copy.deepcopy(creds)
            new_creds["claudeAiOauth"]["accessToken"] = token_data["access_token"]
            new_creds["claudeAiOauth"]["refreshToken"] = token_data.get("refresh_token", refresh_token)
            new_creds["claudeAiOauth"]["expiresAt"] = int(time.time() * 1000) + (
                token_data.get("expires_in", 3600) * 1000
            )
            return new_creds

        console.print(f"[yellow]Direct token refresh failed: {response.status_code}[/yellow]")
        return None

    except Exception as exc:
        console.print(f"[yellow]Direct token refresh error: {exc}[/yellow]")
        return None


def refresh_token(credentials_json: str, force: bool = False) -> Dict:
    """Ensure credentials contain a fresh access token."""
    creds = json.loads(credentials_json)
    expires_at = creds.get("claudeAiOauth", {}).get("expiresAt", 0)
    if not force and expires_at - 600_000 > int(time.time() * 1000):
        return creds

    console.print("[yellow]Refreshing token...[/yellow]")
    refreshed = refresh_token_direct(credentials_json)
    if refreshed:
        console.print("[green]Token refreshed successfully[/green]")
        return refreshed

    console.print("[red]Token refresh failed. The credentials may be expired or invalid.[/red]")
    raise ValueError("Direct token refresh failed. Please re-authenticate.")

