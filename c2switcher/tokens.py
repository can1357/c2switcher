"""Token refresh helpers."""

from __future__ import annotations

import copy
import json
import random
import subprocess
import time
from typing import Dict, Optional

import requests

from .constants import console
from .sandbox import SandboxEnvironment


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


def _refresh_token_sandbox(
    credentials_json: str, account_uuid: Optional[str] = None, account_info: Optional[Dict] = None
) -> Dict:
    """Refresh token by invoking Claude inside a sandboxed HOME directory."""
    creds = json.loads(credentials_json)
    expires_at = creds.get("claudeAiOauth", {}).get("expiresAt", 0)

    creds_to_refresh = copy.deepcopy(creds)
    fake_expiry = int(time.time() * 1000) + 60_000
    creds_to_refresh["claudeAiOauth"]["expiresAt"] = fake_expiry

    if account_uuid is None:
        import hashlib

        creds_hash = hashlib.sha256(credentials_json.encode()).hexdigest()[:16]
        account_uuid = f"bootstrap-{creds_hash}"

    sandbox = SandboxEnvironment(account_uuid, creds_to_refresh, account_info)
    with sandbox as env:
        use_fallback = random.random() < 0.1

        if not use_fallback:
            try:
                subprocess.run(
                    ["claude", "-p", "/status", "--verbose", "--output-format=json"],
                    timeout=30,
                    capture_output=True,
                    check=False,
                    env=env,
                )

                refreshed_creds = sandbox.get_refreshed_credentials()
                new_expires_at = refreshed_creds.get("claudeAiOauth", {}).get("expiresAt", 0)
                if new_expires_at > expires_at:
                    return refreshed_creds

                console.print("[yellow]Status check didn't refresh token, using fallback...[/yellow]")

            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass

        try:
            subprocess.run(
                ["claude", "-p", "hi", "--model", "haiku"],
                timeout=30,
                capture_output=True,
                check=False,
                env=env,
            )
        except subprocess.TimeoutExpired:
            pass
        except FileNotFoundError:
            console.print("[red]Error: 'claude' command not found. Please ensure Claude Code is installed.[/red]")
            raise

        refreshed_creds = sandbox.get_refreshed_credentials()
        final_expires_at = refreshed_creds.get("claudeAiOauth", {}).get("expiresAt", 0)
        if final_expires_at <= expires_at:
            console.print("[red]Error: Failed to refresh token. The credentials may be revoked or invalid.[/red]")
            raise ValueError(
                "Token refresh failed after multiple attempts. "
                "Please re-authenticate by logging in to Claude Code with this account."
            )

        return refreshed_creds


def refresh_token(
    credentials_json: str,
    account_uuid: Optional[str] = None,
    account_info: Optional[Dict] = None,
    force: bool = False,
) -> Dict:
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

    console.print("[yellow]Direct refresh failed, using Claude Code sandbox method...[/yellow]")
    return _refresh_token_sandbox(credentials_json, account_uuid, account_info)

