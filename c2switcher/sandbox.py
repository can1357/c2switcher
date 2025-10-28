"""Sandboxed Claude Code environment for safe token refreshes."""

from __future__ import annotations

import json
import os
import random
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional

from .constants import console
from .utils import atomic_write_json


class SandboxEnvironment:
    """
    Context manager for isolated Claude Code sandbox environment.

    Creates a temporary HOME directory with Claude configuration inherited
    from the real HOME to avoid prompts for terminal theme and other settings.
    Automatically cleans up on exit.
    """

    def __init__(self, account_uuid: str, credentials: Dict, account_info: Optional[Dict] = None):
        self.account_uuid = account_uuid
        self.credentials = credentials
        self.account_info = account_info
        self.temp_home: Optional[Path] = None
        self.temp_claude_dir: Optional[Path] = None
        self.temp_creds_path: Optional[Path] = None
        self.env: Optional[Dict[str, str]] = None

    def __enter__(self) -> Dict[str, str]:
        self.temp_home = Path.home() / ".c2switcher" / "tmp" / self.account_uuid
        self.temp_claude_dir = self.temp_home / ".claude"
        self.temp_claude_dir.mkdir(parents=True, exist_ok=True)
        self.temp_creds_path = self.temp_claude_dir / ".credentials.json"

        try:
            os.chmod(self.temp_home, 0o700)
            os.chmod(self.temp_claude_dir, 0o700)
        except OSError:
            pass

        atomic_write_json(self.temp_creds_path, self.credentials, preserve_permissions=False)

        sandbox_claude_json = self.temp_home / ".claude.json"

        try:
            claude_json = {
                "numStartups": 4,
                "installMethod": "unknown",
                "autoUpdates": False,
                "tipsHistory": {"git-worktrees": 0},
                "cachedStatsigGates": {
                    "tengu_disable_bypass_permissions_mode": False,
                    "tengu_tool_pear": False,
                },
                "cachedDynamicConfigs": {
                    "tengu-top-of-feed-tip": {
                        "tip": "",
                        "color": "dim",
                    }
                },
                "fallbackAvailableWarningThreshold": 0.5,
                "firstStartTime": f"{datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}",
                "sonnet45MigrationComplete": True,
                "changelogLastFetched": int(time.time() * 1000) - random.randint(3_600_000, 86_400_000),
                "claudeCodeFirstTokenDate": f"{(datetime.now(timezone.utc) - timedelta(days=random.randint(30, 180))).isoformat()}Z",
                "hasCompletedOnboarding": True,
                "lastOnboardingVersion": "2.0.25",
                "hasOpusPlanDefault": False,
                "lastReleaseNotesSeen": "2.0.25",
                "subscriptionNoticeCount": 0,
                "hasAvailableSubscription": False,
                "bypassPermissionsModeAccepted": True,
            }

            if self.account_info:
                claude_json["oauthAccount"] = {
                    "accountUuid": self.account_info.get("uuid", self.account_uuid),
                    "emailAddress": self.account_info.get("email", ""),
                    "organizationUuid": self.account_info.get("org_uuid", ""),
                    "displayName": self.account_info.get("display_name", ""),
                    "organizationBillingType": self.account_info.get("billing_type", ""),
                    "organizationRole": "admin",
                    "workspaceRole": None,
                    "organizationName": self.account_info.get("org_name", ""),
                }

            atomic_write_json(sandbox_claude_json, claude_json, preserve_permissions=False)
            os.chmod(sandbox_claude_json, 0o600)
        except (PermissionError, OSError):
            pass

        sandbox_settings = self.temp_claude_dir / "settings.json"

        try:
            settings = {
                "$schema": "https://json.schemastore.org/claude-code-settings.json",
                "alwaysThinkingEnabled": True,
            }
            atomic_write_json(sandbox_settings, settings, preserve_permissions=False)
            os.chmod(sandbox_settings, 0o600)
        except (PermissionError, OSError):
            pass

        self.env = os.environ.copy()
        self.env["HOME"] = str(self.temp_home.resolve())
        self.env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"

        return self.env

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.temp_home and self.temp_home.exists():
            try:
                shutil.rmtree(self.temp_home)
            except Exception as exc:
                console.print(
                    f"[yellow]Warning: Failed to clean up sandbox directory {self.temp_home}: {exc}[/yellow]"
                )
        return False

    def get_refreshed_credentials(self) -> Dict:
        try:
            with open(self.temp_creds_path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            return self.credentials
