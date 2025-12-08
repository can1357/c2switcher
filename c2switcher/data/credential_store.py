"""Token refresh and credentials persistence."""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from typing import Dict

import requests

from typing import TYPE_CHECKING

from ..constants import CLAUDE_DIR, CREDENTIALS_PATH, console
from ..core.errors import InvalidCredentials, TokenUnavailable

if TYPE_CHECKING:
    from ..core.models import Account


class CredentialStore:
    """
    Manages OAuth token refresh and credential file synchronization.

    Responsibilities:
    - Refresh access tokens via Anthropic OAuth endpoint
    - Write credentials to ~/.claude/.credentials.json
    - Provide dry-run mode for testing
    """

    OAUTH_ENDPOINT = 'https://console.anthropic.com/v1/oauth/token'
    CLIENT_ID = '9d1c250a-e61b-44d9-88ed-5944d1962f5e'
    REFRESH_BUFFER_MS = 600_000  # 10 minutes

    def __init__(self, credentials_path: Path = CREDENTIALS_PATH):
        self.credentials_path = credentials_path

    def parse_credentials(self, credentials_json: str) -> Dict:
        """Parse credentials JSON with validation."""
        try:
            creds = json.loads(credentials_json)
            if not isinstance(creds, dict):
                raise InvalidCredentials('Credentials must be a JSON object')
            if 'claudeAiOauth' not in creds:
                raise InvalidCredentials('Missing claudeAiOauth field')
            return creds
        except json.JSONDecodeError as exc:
            raise InvalidCredentials(f'Invalid JSON: {exc}')

    def is_token_fresh(self, credentials: Dict, force: bool = False) -> bool:
        """Check if access token is still valid."""
        if force:
            return False

        expires_at = credentials.get('claudeAiOauth', {}).get('expiresAt', 0)
        now_ms = int(time.time() * 1000)
        return expires_at - self.REFRESH_BUFFER_MS > now_ms

    def refresh_access_token(self, credentials_json: str, force: bool = False) -> Dict:
        """
        Refresh OAuth access token.

        Returns updated credentials dict with new access token.
        Raises TokenUnavailable if refresh fails.
        """
        creds = self.parse_credentials(credentials_json)

        if self.is_token_fresh(creds, force):
            return creds

        oauth = creds.get('claudeAiOauth', {})
        refresh_token = oauth.get('refreshToken')

        if not refresh_token:
            raise TokenUnavailable('No refresh token available')

        console.print('[yellow]Refreshing token...[/yellow]')

        try:
            response = requests.post(
                self.OAUTH_ENDPOINT,
                json={
                    'grant_type': 'refresh_token',
                    'refresh_token': refresh_token,
                    'client_id': self.CLIENT_ID,
                },
                timeout=10,
            )

            if response.status_code != 200:
                raise TokenUnavailable(f'OAuth endpoint returned {response.status_code}')

            token_data = response.json()

            new_creds = copy.deepcopy(creds)
            new_creds['claudeAiOauth']['accessToken'] = token_data['access_token']
            new_creds['claudeAiOauth']['refreshToken'] = token_data.get('refresh_token', refresh_token)
            new_creds['claudeAiOauth']['expiresAt'] = int(time.time() * 1000) + (
                token_data.get('expires_in', 3600) * 1000
            )

            console.print('[green]Token refreshed successfully[/green]')
            return new_creds

        except requests.RequestException as exc:
            raise TokenUnavailable(f'OAuth request failed: {exc}')

    def write_credentials(self, credentials: Dict):
        """Write credentials to ~/.claude/.credentials.json."""
        CLAUDE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)

        try:
            import os

            os.chmod(CLAUDE_DIR, 0o700)
        except OSError:
            pass

        temp_path = self.credentials_path.with_suffix('.tmp')
        try:
            with temp_path.open('w', encoding='utf-8') as f:
                json.dump(credentials, f, indent=2)

            import os

            os.chmod(temp_path, 0o600)

            temp_path.replace(self.credentials_path)

        except Exception:
            if temp_path.exists():
                temp_path.unlink()
            raise

    def write_credentials_for_account(self, account: 'Account', oauth_credentials: Dict):
        """
        Write credentials for account, using API key format when available.

        When the account has an api_key, writes a simplified format:
        {"claudeAiOauth": {"accessToken": "<api_key>", "scopes": ["user:inference"]}}

        Otherwise writes the full OAuth credentials.
        """
        if account.api_key:
            credentials = {
                'claudeAiOauth': {
                    'accessToken': account.api_key,
                    'scopes': ['user:inference'],
                }
            }
        else:
            credentials = oauth_credentials

        self.write_credentials(credentials)

    def refresh_and_persist(self, credentials_json: str, force: bool = False, dry_run: bool = False) -> Dict:
        """
        Refresh token and write to disk.

        Args:
           credentials_json: Current credentials JSON string
           force: Force refresh even if token is fresh
           dry_run: Skip writing to disk (for testing)

        Returns:
           Updated credentials dict
        """
        refreshed = self.refresh_access_token(credentials_json, force=force)

        if not dry_run:
            self.write_credentials(refreshed)

        return refreshed

    def get_access_token(self, credentials_json: str, force: bool = False) -> str:
        """
        Extract access token, refreshing if necessary.

        Returns:
           Access token string

        Raises:
           TokenUnavailable: If token cannot be obtained
        """
        refreshed = self.refresh_access_token(credentials_json, force=force)
        token = refreshed.get('claudeAiOauth', {}).get('accessToken')

        if not token:
            raise TokenUnavailable('No access token in refreshed credentials')

        return token
