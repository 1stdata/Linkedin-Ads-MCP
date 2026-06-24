#!/usr/bin/env python3
"""
Standalone automated bid pacer for LinkedIn Ads campaigns.

Reads bid-pacing rules from bid_pacing_rules.json, and for each rule adjusts the
campaign's MANUAL bid (unitCost) so it paces toward its daily budget without
letting the *account's* month exceed a monthly cap. Changes auto-apply within
guardrails and are logged to bid_pacing_history.json.

The control logic lives in linkedin_ads_server._run_bid_pacer_engine so the MCP
tool (`run_bid_pacer`) and this script behave identically. Importing the server
module does NOT start the MCP server (that only happens under __main__ there),
so this is safe to run from cron.

Usage:
    python bid_pacer.py            # evaluate + apply
    python bid_pacer.py --dry-run  # preview only, no changes

Designed to be run once each morning via cron, e.g.:
    # Every day at 09:00 (server local time)
    0 9 * * * cd /path/to/mcp-linkedin-ads-main && python bid_pacer.py >> bid_pacer.log 2>&1

Requires:
    - .env with LINKEDIN_ACCESS_TOKEN (or LINKEDIN_REFRESH_TOKEN + client creds)
    - bid_pacing_rules.json (managed via the add_bid_pacing_rule MCP tool)
"""

import json
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [bid_pacer] %(levelname)s %(message)s",
)
logger = logging.getLogger("bid_pacer")


def main() -> None:
    dry_run = "--dry-run" in sys.argv or "-n" in sys.argv

    try:
        import linkedin_ads_server as srv
    except Exception as e:  # pragma: no cover
        logger.error("Could not import linkedin_ads_server: %s", e)
        sys.exit(1)

    rules = srv._load_bid_pacing_rules().get("rules", [])
    if not rules:
        logger.info("No bid-pacing rules configured — nothing to do.")
        return

    logger.info("Running bid pacer over %d rule(s)%s...",
                len(rules), " (DRY RUN)" if dry_run else "")
    summary = srv._run_bid_pacer_engine(dry_run=dry_run)

    for d in summary["results"]:
        if "error" in d:
            logger.error("Campaign %s: %s", d.get("campaign_id"), d["error"])
            continue
        if d.get("skipped"):
            logger.info("Campaign %s skipped: %s", d.get("campaign_id"), d["skipped"])
            continue
        verb = "APPLIED" if d.get("applied") else ("WOULD SET" if d.get("will_change") else "HOLD")
        logger.info("%s %s (%s): $%.2f -> $%.2f (%+.1f%%) | %s",
                    verb, d.get("campaign_name", "")[:38], d.get("campaign_id"),
                    d["current_bid"], d["recommended_bid"], d["pct_change"], d["reason"])
        if d.get("apply_error"):
            logger.error("  apply error: %s", d["apply_error"])

    logger.info("Bid pacer run complete (%s).", summary["ran_at"])


if __name__ == "__main__":
    main()
