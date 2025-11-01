"""Domain-specific exceptions for c2switcher."""

from __future__ import annotations


class C2SwitcherError(Exception):
   """Base exception for all c2switcher domain errors."""
   pass


class NoAccountsAvailable(C2SwitcherError):
   """No accounts registered or all exhausted."""
   pass


class TokenUnavailable(C2SwitcherError):
   """Could not obtain valid access token."""
   pass


class SessionRegistrationError(C2SwitcherError):
   """Failed to register or track session."""
   pass


class AccountNotFound(C2SwitcherError):
   """Account identifier does not match any registered account."""
   pass


class InvalidCredentials(C2SwitcherError):
   """Credentials JSON is malformed or missing required fields."""
   pass


class UsageFetchError(C2SwitcherError):
   """Failed to retrieve usage data from API."""
   pass


class ProfileFetchError(C2SwitcherError):
   """Failed to retrieve profile data from API."""
   pass
