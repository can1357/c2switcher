"""Account management service layer."""

from __future__ import annotations

from typing import List, Optional, Tuple

from ..core.errors import AccountNotFound, InvalidCredentials, ProfileFetchError
from ..core.models import Account
from ..data.credential_store import CredentialStore
from ..data.store import Store
from ..infrastructure.api import ClaudeAPI


class AccountService:
    """
    Orchestrates account operations.

    Responsibilities:
    - Add/list/remove accounts
    - Profile fetching and persistence
    - Credential validation
    """

    def __init__(self, store: Store, credential_store: CredentialStore):
        self.store = store
        self.credential_store = credential_store

    def add_account(self, credentials_json: str, nickname: Optional[str] = None) -> Tuple[Account, bool]:
        """
        Register new account or update existing.

        Returns:
           (Account, is_new) tuple

        Raises:
           InvalidCredentials: If credentials are malformed
           ProfileFetchError: If profile fetch fails
        """
        # Validate and refresh credentials
        self.credential_store.parse_credentials(credentials_json)
        refreshed = self.credential_store.refresh_access_token(credentials_json, force=False)
        token = refreshed.get('claudeAiOauth', {}).get('accessToken')

        if not token:
            raise InvalidCredentials('No access token available')

        # Fetch profile
        try:
            profile = ClaudeAPI.get_profile(token)
        except Exception as exc:
            raise ProfileFetchError(f'Failed to fetch profile: {exc}')

        # Save account
        account, is_new = self.store.save_account(profile, refreshed, nickname=nickname)

        return account, is_new

    def list_accounts(self) -> List[Account]:
        """Retrieve all registered accounts."""
        return self.store.list_accounts()

    def get_account(self, identifier: str) -> Account:
        """
        Retrieve account by identifier.

        Args:
           identifier: index, uuid, nickname, or email

        Raises:
           AccountNotFound: If no match found
        """
        account = self.store.get_account_by_identifier(identifier)
        if not account:
            raise AccountNotFound(f'No account found for: {identifier}')
        return account

    def remove_account(self, identifier: str):
        """
        Remove account from database.

        Also cleans up associated sessions.
        """
        account = self.get_account(identifier)

        # Mark active sessions ended
        active_sessions = self.store.list_active_sessions()
        for session in active_sessions:
            if session.account_uuid == account.uuid:
                self.store.mark_session_ended(session.session_id)

        # Delete account (requires adding delete method to store)
        # For now, this is a placeholder
        raise NotImplementedError('Account deletion not yet implemented')
