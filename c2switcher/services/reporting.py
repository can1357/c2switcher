"""Reporting data preparation service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from ..core.models import Account
from ..data.store import Store


@dataclass
class UsageOverview:
    """Aggregated usage metrics for display."""

    accounts: List[Account]
    usage_by_account: Dict[str, Dict]  # account_uuid -> usage dict
    active_sessions: Dict[str, int]  # account_uuid -> count


@dataclass
class SessionSummary:
    """Session statistics for reporting."""

    total_sessions: int
    active_sessions: int
    ended_sessions: int
    avg_duration_seconds: Optional[float]


class ReportingService:
    """
    Prepares data for reports and visualizations.

    Responsibilities:
    - Aggregate usage data across accounts
    - Compute session statistics
    - Return plain Python structures for rendering
    """

    def __init__(self, store: Store):
        self.store = store

    def get_usage_overview(self) -> UsageOverview:
        """
        Build overview of all accounts with current usage.

        Returns:
           UsageOverview with accounts, usage, and session counts
        """
        accounts = self.store.list_accounts()
        usage_by_account: Dict[str, Dict] = {}

        for account in accounts:
            usage = self.store.get_recent_usage(account.uuid, max_age_seconds=300)
            if usage:
                usage_by_account[account.uuid] = {
                    'five_hour': {
                        'utilization': usage.five_hour.utilization,
                        'resets_at': usage.five_hour.resets_at,
                    },
                    'seven_day': {
                        'utilization': usage.seven_day.utilization,
                        'resets_at': usage.seven_day.resets_at,
                    },
                    'seven_day_opus': {
                        'utilization': usage.seven_day_opus.utilization,
                        'resets_at': usage.seven_day_opus.resets_at,
                    },
                    'seven_day_sonnet': {
                        'utilization': usage.seven_day_sonnet.utilization,
                        'resets_at': usage.seven_day_sonnet.resets_at,
                    },
                    'cache_age_seconds': usage.cache_age_seconds,
                }

        active_sessions = self.store.get_active_session_counts()

        return UsageOverview(
            accounts=accounts,
            usage_by_account=usage_by_account,
            active_sessions=active_sessions,
        )

    def get_session_summary(self) -> SessionSummary:
        """
        Compute session statistics.

        Returns:
           SessionSummary with counts and duration averages
        """
        active_sessions = self.store.list_active_sessions()
        # For ended sessions, we'd need to add a store method
        # This is a simplified version

        return SessionSummary(
            total_sessions=len(active_sessions),
            active_sessions=len(active_sessions),
            ended_sessions=0,  # Placeholder
            avg_duration_seconds=None,
        )

    def get_accounts_with_usage(self) -> List[tuple[Account, Optional[Dict]]]:
        """
        Fetch accounts paired with their latest usage.

        Returns:
           List of (Account, usage_dict) tuples
        """
        accounts = self.store.list_accounts()
        result: List[tuple[Account, Optional[Dict]]] = []

        for account in accounts:
            usage = self.store.get_recent_usage(account.uuid, max_age_seconds=300)
            usage_dict = None

            if usage:
                usage_dict = {
                    'five_hour': {
                        'utilization': usage.five_hour.utilization,
                        'resets_at': usage.five_hour.resets_at,
                    },
                    'seven_day': {
                        'utilization': usage.seven_day.utilization,
                        'resets_at': usage.seven_day.resets_at,
                    },
                    'seven_day_opus': {
                        'utilization': usage.seven_day_opus.utilization,
                        'resets_at': usage.seven_day_opus.resets_at,
                    },
                    'seven_day_sonnet': {
                        'utilization': usage.seven_day_sonnet.utilization,
                        'resets_at': usage.seven_day_sonnet.resets_at,
                    },
                }

            result.append((account, usage_dict))

        return result
