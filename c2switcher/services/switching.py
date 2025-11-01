"""Account switching orchestration."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

from ..constants import CACHE_TTL_SECONDS, STALE_CACHE_SECONDS, console
from ..core.errors import NoAccountsAvailable, UsageFetchError
from ..core.load_balancing import (
   build_candidate,
   needs_refresh,
   select_best_candidate,
   select_top_similar_candidates,
)
from ..core.models import Account, Candidate, SelectionDecision, UsageSnapshot
from ..data.credential_store import CredentialStore
from ..data.store import Store
from ..infrastructure.api import ClaudeAPI
from ..services.sessions import SessionService


class SwitchingService:
   """
   Orchestrates account selection and credential switching.

   Responsibilities:
   - Load-balanced account selection
   - Usage cache hydration
   - Credential file writes
   - Session reuse
   """

   def __init__(
      self, store: Store, credential_store: CredentialStore, session_service: SessionService
   ):
      self.store = store
      self.credential_store = credential_store
      self.session_service = session_service

   def select_optimal(
      self, session_id: Optional[str] = None, token_only: bool = False, dry_run: bool = False
   ) -> SelectionDecision:
      """
      Select best account using load balancing.

      Args:
         session_id: Optional session to bind account to
         token_only: Skip writing full credentials file (still refresh tokens unless dry-run)
         dry_run: Skip credential writes/DB persistence for the final switch step

      Returns:
         SelectionDecision with account and diagnostics

      Raises:
         NoAccountsAvailable: If no usable accounts
      """
      # Cleanup sessions periodically
      self.session_service.maybe_cleanup()

      # Try session reuse
      if session_id:
         reused = self._try_reuse_session(session_id)
         if reused:
            if not dry_run:
               refreshed_creds = reused.account.get_credentials()
               if not token_only:
                  self.credential_store.write_credentials(refreshed_creds)
            return reused

      # Get all accounts
      accounts = self.store.list_accounts()
      if not accounts:
         raise NoAccountsAvailable("No accounts registered")

      # Collect cached usage
      usage_map, missing_accounts = self._collect_cached_usage(accounts)

      # Fetch missing usage
      if missing_accounts:
         fetched = self._fetch_usage_batch(missing_accounts, label="initial")
         usage_map.update(fetched)

      if not usage_map:
         raise NoAccountsAvailable("Could not fetch usage for any account")

      # Build candidates
      active_counts = self.store.get_active_session_counts()
      recent_counts = self.store.get_recent_session_counts(minutes=5)
      burst_cache: Dict[str, float] = {}
      refreshed_ids: Set[str] = set()

      candidates = self._build_candidates(
         accounts, usage_map, active_counts, recent_counts, burst_cache, refreshed_ids
      )

      # Refresh stale high-priority candidates
      refresh_accounts = []
      for acc in accounts:
         if acc.uuid not in usage_map:
            continue
         candidate = self._find_candidate(candidates, acc.uuid)
         if candidate and needs_refresh(candidate):
            refresh_accounts.append(acc)

      if refresh_accounts:
         refreshed = self._fetch_usage_batch(refresh_accounts, label="refresh")
         usage_map.update(refreshed)
         for uuid in refreshed.keys():
            refreshed_ids.add(uuid)

         # Rebuild candidates with fresh data
         candidates = self._build_candidates(
            accounts, usage_map, active_counts, recent_counts, burst_cache, refreshed_ids
         )

      # Select best with round-robin for similar candidates
      similar = select_top_similar_candidates(candidates)
      if not similar:
         raise NoAccountsAvailable("All accounts exhausted")

      selected = self._choose_round_robin(similar, active_counts, recent_counts)
      if not selected:
         selected = similar[0]

      # Bind session (skip during dry-run)
      if session_id and not dry_run:
         self.store.assign_session_to_account(session_id, selected.account.uuid)

      refreshed_creds = None

      if dry_run:
         # Keep current credentials in-memory for downstream consumers
         refreshed_creds = selected.account.get_credentials()
      else:
         # Refresh credentials to ensure valid token for switching/token-only flows
         refreshed_creds = self.credential_store.refresh_access_token(
            selected.account.credentials_json
         )

         if not token_only:
            self.credential_store.write_credentials(refreshed_creds)

         # Update stored credentials if changed
         if refreshed_creds != selected.account.get_credentials():
            self.store.update_credentials(selected.account.uuid, refreshed_creds)
            # Update in-memory Account so returned SelectionDecision has fresh credentials
            selected.account.credentials_json = json.dumps(refreshed_creds)

      return SelectionDecision.from_candidate(selected, reused=False)

   def switch_to(self, identifier: str, token_only: bool = False) -> Account:
      """
      Switch to specific account by identifier.

      Args:
         identifier: index, uuid, nickname, or email
         token_only: Skip writing full credentials file

      Returns:
         Selected Account
      """
      account = self.store.get_account_by_identifier(identifier)
      if not account:
         raise NoAccountsAvailable(f"Account not found: {identifier}")

      refreshed_creds = self.credential_store.refresh_access_token(account.credentials_json)

      if not token_only:
         self.credential_store.write_credentials(refreshed_creds)

      # Update stored credentials if changed
      if refreshed_creds != account.get_credentials():
         self.store.update_credentials(account.uuid, refreshed_creds)
         # Update in-memory Account so caller gets fresh credentials
         account.credentials_json = json.dumps(refreshed_creds)

      return account

   def _choose_round_robin(
      self, candidates: List[Candidate], active_counts: Dict[str, int], recent_counts: Dict[str, int]
   ) -> Optional[Candidate]:
      """
      Select from similar candidates using round-robin.

      Prioritizes:
      1. Candidates with fewest active sessions
      2. Candidates with fewest recent sessions
      3. Round-robin based on last-selected UUID stored in DB
      """
      if len(candidates) == 1:
         return candidates[0]

      # Filter by minimum active sessions
      min_active = min(active_counts.get(c.account.uuid, 0) for c in candidates)
      pool = [c for c in candidates if active_counts.get(c.account.uuid, 0) == min_active]

      # Filter by minimum recent sessions
      min_recent = min(recent_counts.get(c.account.uuid, 0) for c in pool)
      pool = [c for c in pool if recent_counts.get(c.account.uuid, 0) == min_recent]

      # Sort by index for deterministic ordering
      pool.sort(key=lambda c: c.account.index_num)

      # Round-robin based on DB state
      window = f"tier_{pool[0].tier}"
      last_uuid = self.store.get_round_robin_last(window)

      # Find next in rotation
      candidate_uuids = [c.account.uuid for c in pool]
      next_idx = 0

      if last_uuid and last_uuid in candidate_uuids:
         for idx, cand in enumerate(pool):
            if cand.account.uuid == last_uuid:
               next_idx = (idx + 1) % len(pool)
               break

      selected = pool[next_idx]

      # Update DB state
      self.store.set_round_robin_last(window, selected.account.uuid)

      return selected

   def _try_reuse_session(self, session_id: str) -> Optional[SelectionDecision]:
      """Attempt to reuse existing session account."""
      result = self.store.get_session_account(session_id)
      if not result:
         return None

      session, account = result

      # Check if account still usable
      try:
         usage = self._fetch_usage_for_account(account)
         opus = usage.seven_day_opus.utilization
         overall = usage.seven_day.utilization

         opus_ok = opus is None or float(opus) < 99
         overall_ok = overall is None or float(overall) < 99

         if opus_ok and overall_ok:
            # Build decision from reuse
            active_counts = self.store.get_active_session_counts()
            recent_counts = self.store.get_recent_session_counts(minutes=5)
            burst_buffer = self.store.get_burst_percentile(account.uuid)

            candidate = build_candidate(
               account,
               usage,
               burst_buffer,
               active_counts.get(account.uuid, 0),
               recent_counts.get(account.uuid, 0),
               refreshed=True,
            )

            if candidate:
               return SelectionDecision.from_candidate(candidate, reused=True)

      except Exception:
         pass

      return None

   def _collect_cached_usage(
      self, accounts: List[Account]
   ) -> tuple[Dict[str, UsageSnapshot], List[Account]]:
      """Collect cached usage and identify missing accounts."""
      usage_map: Dict[str, UsageSnapshot] = {}
      missing: List[Account] = []

      for account in accounts:
         cached = self.store.get_recent_usage(account.uuid, max_age_seconds=CACHE_TTL_SECONDS)
         if cached:
            usage_map[account.uuid] = cached
         else:
            missing.append(account)

      return usage_map, missing

   def _fetch_usage_for_account(self, account: Account) -> UsageSnapshot:
      """Fetch live usage for single account."""
      usage_data, refreshed_creds = self._refresh_usage_payload(account)
      return self._persist_usage_result(account, usage_data, refreshed_creds)

   def _fetch_usage_batch(
      self, accounts: List[Account], label: str = "batch"
   ) -> Dict[str, UsageSnapshot]:
      """Fetch usage in parallel for multiple accounts."""
      if not accounts:
         return {}

      results: Dict[str, UsageSnapshot] = {}
      max_workers = min(len(accounts), 10)

      with ThreadPoolExecutor(max_workers=max_workers) as executor:
         future_map = {executor.submit(self._refresh_usage_payload, acc): acc for acc in accounts}

         for future in as_completed(future_map):
            account = future_map[future]
            try:
               usage_data, refreshed_creds = future.result()
               usage = self._persist_usage_result(account, usage_data, refreshed_creds)
               results[account.uuid] = usage
            except Exception as exc:
               console.print(
                  f"[yellow]Warning: Could not fetch usage for {account.email} ({label}): {exc}[/yellow]"
               )

      return results

   def _refresh_usage_payload(self, account: Account) -> tuple[Dict, Dict]:
      """Fetch usage data and refreshed credentials without touching persistence."""
      refreshed_creds = self.credential_store.refresh_access_token(account.credentials_json)
      token = refreshed_creds.get("claudeAiOauth", {}).get("accessToken")

      if not token:
         raise UsageFetchError("No access token")

      usage_data = ClaudeAPI.get_usage(token)
      usage_data["_cache_source"] = "live"
      usage_data["_cache_age_seconds"] = 0.0
      usage_data["_queried_at"] = datetime.now(timezone.utc).isoformat()

      return usage_data, refreshed_creds

   def _persist_usage_result(
      self, account: Account, usage_data: Dict, refreshed_creds: Dict
   ) -> UsageSnapshot:
      """Persist usage + credentials updates and return snapshot."""
      usage_to_store = {k: v for k, v in usage_data.items() if not k.startswith("_")}
      self.store.save_usage(account.uuid, usage_to_store)

      current_creds = account.get_credentials()
      if refreshed_creds != current_creds:
         self.store.update_credentials(account.uuid, refreshed_creds)
         account.credentials_json = json.dumps(refreshed_creds)

      return UsageSnapshot.from_api_response(account.uuid, usage_data, source="live")

   def _build_candidates(
      self,
      accounts: List[Account],
      usage_map: Dict[str, UsageSnapshot],
      active_counts: Dict[str, int],
      recent_counts: Dict[str, int],
      burst_cache: Dict[str, float],
      refreshed_ids: Set[str],
   ) -> List[Candidate]:
      """Build candidate list with scoring."""
      candidates: List[Candidate] = []

      for account in accounts:
         usage = usage_map.get(account.uuid)
         if not usage:
            continue

         burst_buffer = burst_cache.get(account.uuid)
         if burst_buffer is None or account.uuid in refreshed_ids:
            burst_buffer = self.store.get_burst_percentile(account.uuid)
            burst_cache[account.uuid] = burst_buffer

         candidate = build_candidate(
            account,
            usage,
            burst_buffer,
            active_counts.get(account.uuid, 0),
            recent_counts.get(account.uuid, 0),
            refreshed=account.uuid in refreshed_ids,
         )

         if candidate:
            candidates.append(candidate)

      return candidates

   def _find_candidate(self, candidates: List[Candidate], account_uuid: str) -> Optional[Candidate]:
      """Find candidate by account UUID."""
      for cand in candidates:
         if cand.account.uuid == account_uuid:
            return cand
      return None
