#!/usr/bin/env python3
"""
Standalone weekday-only scheduler for LinkedIn Ads campaigns.

Reads scheduling rules from schedules.json, checks the current day and time,
and pauses or resumes campaigns accordingly via the LinkedIn Marketing API.

Usage:
    python scheduler.py

Designed to be run via cron, e.g.:
    # Every hour
    0 * * * * cd /path/to/mcp-linkedin-ads-main && python scheduler.py

    # Specific times: Friday 6PM and Monday 6AM (Eastern)
    0 18 * * 5 cd /path/to/mcp-linkedin-ads-main && python scheduler.py
    0 6 * * 1 cd /path/to/mcp-linkedin-ads-main && python scheduler.py

Requires:
    - .env file with LINKEDIN_ACCESS_TOKEN (or LINKEDIN_REFRESH_TOKEN + client credentials)
    - schedules.json with weekday_only rules (managed via MCP tools)
"""

import json
import logging
import os
import sys
from datetime import datetime

import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEDULES_FILE = os.path.join(SCRIPT_DIR, "schedules.json")

LINKEDIN_API_BASE = "https://api.linkedin.com/rest"
LINKEDIN_OAUTH_TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
API_VERSION = os.environ.get("LINKEDIN_API_VERSION", "202605")

ACCESS_TOKEN = os.environ.get("LINKEDIN_ACCESS_TOKEN", "")
REFRESH_TOKEN = os.environ.get("LINKEDIN_REFRESH_TOKEN", "")
CLIENT_ID = os.environ.get("LINKEDIN_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("LINKEDIN_CLIENT_SECRET", "")
TOKEN_PATH = os.environ.get("LINKEDIN_TOKEN_PATH", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [scheduler] %(levelname)s %(message)s",
)
logger = logging.getLogger("scheduler")

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def get_access_token() -> str:
    """Resolve a valid access token using token file, env var, or refresh."""
    # 1. Token file
    if TOKEN_PATH and os.path.exists(TOKEN_PATH):
        try:
            with open(TOKEN_PATH, "r") as f:
                data = json.load(f)
            token = data.get("access_token", "")
            expires_at = data.get("expires_at", 0)
            if token and (expires_at == 0 or __import__("time").time() < expires_at - 60):
                return token
            # Try refresh
            rt = data.get("refresh_token") or REFRESH_TOKEN
            if rt and CLIENT_ID and CLIENT_SECRET:
                return _refresh(rt)
        except Exception as e:
            logger.warning("Could not read token file: %s", e)

    # 2. Env var
    if ACCESS_TOKEN:
        return ACCESS_TOKEN

    # 3. Refresh from env
    if REFRESH_TOKEN and CLIENT_ID and CLIENT_SECRET:
        return _refresh(REFRESH_TOKEN)

    logger.error("No LinkedIn access token available.")
    sys.exit(1)


def _refresh(refresh_token: str) -> str:
    """Refresh the access token and persist it."""
    resp = requests.post(LINKEDIN_OAUTH_TOKEN_URL, data={
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    })
    if resp.status_code != 200:
        logger.error("Token refresh failed (%d): %s", resp.status_code, resp.text)
        sys.exit(1)
    data = resp.json()
    data["expires_at"] = __import__("time").time() + data.get("expires_in", 5184000)
    if "refresh_token" not in data:
        data["refresh_token"] = refresh_token
    if TOKEN_PATH:
        try:
            os.makedirs(os.path.dirname(TOKEN_PATH) or ".", exist_ok=True)
            with open(TOKEN_PATH, "w") as f:
                json.dump(data, f, indent=2)
        except IOError as e:
            logger.warning("Could not save token file: %s", e)
    return data["access_token"]


# ---------------------------------------------------------------------------
# API helper
# ---------------------------------------------------------------------------

def set_campaign_status(account_id: str, campaign_id: str, status: str, token: str) -> bool:
    """Set a campaign's status via LinkedIn partial update. Returns True on success."""
    url = f"{LINKEDIN_API_BASE}/adAccounts/{account_id}/adCampaigns/{campaign_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Linkedin-Version": API_VERSION,
        "X-Restli-Protocol-Version": "2.0.0",
        "Content-Type": "application/json",
        "X-RestLi-Method": "PARTIAL_UPDATE",
    }
    body = {"patch": {"$set": {"status": status}}}
    resp = requests.post(url, headers=headers, json=body)
    if resp.status_code >= 400:
        logger.error(
            "Failed to set campaign %s to %s: %d %s",
            campaign_id, status, resp.status_code, resp.text,
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_scheduler() -> None:
    """Main scheduler logic: read rules, evaluate, pause/resume."""
    if not os.path.exists(SCHEDULES_FILE):
        logger.info("No schedules.json found at %s — nothing to do.", SCHEDULES_FILE)
        return

    with open(SCHEDULES_FILE, "r") as f:
        schedules = json.load(f)

    rules = schedules.get("weekday_only", [])
    if not rules:
        logger.info("No weekday-only rules configured.")
        return

    token = get_access_token()
    logger.info("Processing %d weekday-only rule(s)...", len(rules))

    for rule in rules:
        account_id = rule["account_id"]
        campaign_id = rule["campaign_id"]
        campaign_name = rule.get("campaign_name", campaign_id)
        tz_name = rule.get("timezone", "UTC")
        resume_time = rule.get("resume_time", "06:00")
        pause_time = rule.get("pause_time", "18:00")

        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            logger.warning("Invalid timezone '%s' for campaign %s — skipping.", tz_name, campaign_id)
            continue

        now = datetime.now(tz)
        weekday = now.weekday()  # 0=Mon, 6=Sun
        current_time = now.strftime("%H:%M")

        if weekday >= 5:
            desired = "PAUSED"
        elif weekday == 4 and current_time >= pause_time:
            desired = "PAUSED"
        elif weekday == 0 and current_time < resume_time:
            desired = "PAUSED"
        else:
            desired = "ACTIVE"

        ok = set_campaign_status(account_id, campaign_id, desired, token)
        if ok:
            logger.info(
                "%s -> %s (%s) [tz=%s, %s %s]",
                desired, campaign_name, campaign_id, tz_name, now.strftime("%A"), current_time,
            )
        else:
            logger.error("Failed to set %s for %s (%s)", desired, campaign_name, campaign_id)

    logger.info("Scheduler run complete.")


if __name__ == "__main__":
    run_scheduler()
