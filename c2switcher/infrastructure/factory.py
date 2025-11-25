"""Service factory for dependency injection."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..constants import CREDENTIALS_PATH, DB_PATH
from ..data.credential_store import CredentialStore
from ..data.store import Store
from ..services.accounts import AccountService
from ..services.reporting import ReportingService
from ..services.sessions import SessionService
from ..services.switching import SwitchingService


class ServiceFactory:
    """Factory for creating service instances with dependencies."""

    def __init__(self, db_path: Path = DB_PATH, credentials_path: Path = CREDENTIALS_PATH):
        self.db_path = db_path
        self.credentials_path = credentials_path
        self._store: Optional[Store] = None
        self._credential_store: Optional[CredentialStore] = None

    def get_store(self) -> Store:
        """Get or create Store instance."""
        if self._store is None:
            self._store = Store(self.db_path)
            # Migrate legacy round-robin state on first access
            self._store.migrate_legacy_round_robin_state()
        return self._store

    def get_credential_store(self) -> CredentialStore:
        """Get or create CredentialStore instance."""
        if self._credential_store is None:
            self._credential_store = CredentialStore(self.credentials_path)
        return self._credential_store

    def get_account_service(self) -> AccountService:
        """Get or create AccountService instance."""
        return AccountService(
            store=self.get_store(),
            credential_store=self.get_credential_store(),
        )

    def get_session_service(self) -> SessionService:
        """Get or create SessionService instance."""
        return SessionService(store=self.get_store())

    def get_switching_service(self) -> SwitchingService:
        """Get or create SwitchingService instance."""
        return SwitchingService(
            store=self.get_store(),
            credential_store=self.get_credential_store(),
            session_service=self.get_session_service(),
        )

    def get_reporting_service(self) -> ReportingService:
        """Get or create ReportingService instance."""
        return ReportingService(store=self.get_store())

    def close(self):
        """Close all resources."""
        if self._store:
            self._store.close()

    def __enter__(self) -> ServiceFactory:
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()
