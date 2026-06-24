"""
Flask dashboard for LinkedIn Ads weekday scheduling.

Provides a web UI to view campaigns, toggle weekday scheduling,
pause/resume campaigns, and trigger the scheduler manually.

Reuses API helpers from linkedin_ads_server.py — no duplicated logic.
"""

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta
from functools import wraps

import requests as requests_lib

from flask import Flask, jsonify, render_template, request, Response

# ---------------------------------------------------------------------------
# Railway-aware path configuration (must come before importing helpers)
# ---------------------------------------------------------------------------

if os.environ.get("RAILWAY_ENVIRONMENT"):
    SCHEDULES_FILE = "/data/schedules.json"
    os.environ.setdefault("LINKEDIN_TOKEN_PATH", "/data/linkedin_token.json")
else:
    SCHEDULES_FILE = None  # will use default from linkedin_ads_server

# ---------------------------------------------------------------------------
# Import helpers from the MCP server module
# ---------------------------------------------------------------------------

import linkedin_ads_server as li

# Override the schedules file path if running on Railway
if SCHEDULES_FILE:
    li.SCHEDULES_FILE = SCHEDULES_FILE

# Re-export the helpers we use
linkedin_api_request = li.linkedin_api_request
linkedin_paginated_request = li.linkedin_paginated_request
format_account_urn = li.format_account_urn
extract_id_from_urn = li.extract_id_from_urn
epoch_ms_to_iso = li.epoch_ms_to_iso
get_credentials = li.get_credentials
_load_schedules = li._load_schedules
_save_schedules = li._save_schedules
LINKEDIN_BUSINESS_ACCOUNT_ID = li.LINKEDIN_BUSINESS_ACCOUNT_ID

# Weekly report configuration
WEEKLY_REPORT_ACCOUNT_ID = os.environ.get("WEEKLY_REPORT_ACCOUNT_ID", LINKEDIN_BUSINESS_ACCOUNT_ID)
WEEKLY_REPORT_ENABLED = os.environ.get("WEEKLY_REPORT_ENABLED", "false").lower() == "true"
WEEKLY_REPORT_SLACK_WEBHOOK = os.environ.get("WEEKLY_REPORT_SLACK_WEBHOOK", "")
WEEKLY_REPORT_TIMEZONE = os.environ.get("WEEKLY_REPORT_TIMEZONE", "America/New_York")

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Basic auth
# ---------------------------------------------------------------------------

DASHBOARD_USERNAME = os.environ.get("DASHBOARD_USERNAME", "")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")


def requires_auth(f):
    """Decorator that enforces HTTP Basic Auth when credentials are configured."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not DASHBOARD_USERNAME and not DASHBOARD_PASSWORD:
            return f(*args, **kwargs)
        auth = request.authorization
        if not auth or auth.username != DASHBOARD_USERNAME or auth.password != DASHBOARD_PASSWORD:
            return Response(
                "Authentication required.",
                401,
                {"WWW-Authenticate": 'Basic realm="Dashboard"'},
            )
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
@requires_auth
def index():
    """Serve the dashboard HTML page."""
    return render_template("dashboard.html")


@app.route("/api/accounts")
@requires_auth
def api_accounts():
    """List all accessible LinkedIn Ad Accounts."""
    try:
        elements = linkedin_paginated_request(
            "/adAccounts",
            params={"q": "search"},
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    accounts = []
    for el in elements:
        status = el.get("status", "")
        if status in ("REMOVED",):
            continue
        accounts.append({
            "id": extract_id_from_urn(el.get("id", el.get("reference", ""))),
            "name": el.get("name", ""),
            "status": status,
            "currency": el.get("currency", ""),
        })

    return jsonify({"accounts": accounts, "default": LINKEDIN_BUSINESS_ACCOUNT_ID})


@app.route("/api/campaign-groups")
@requires_auth
def api_campaign_groups():
    """List campaign groups for the account."""
    account_id = request.args.get("account_id", LINKEDIN_BUSINESS_ACCOUNT_ID)
    if not account_id:
        return jsonify({"error": "No account_id provided and LINKEDIN_BUSINESS_ACCOUNT_ID not set"}), 400

    try:
        elements = linkedin_paginated_request(
            f"/adAccounts/{account_id}/adCampaignGroups",
            params={"q": "search"},
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    groups = []
    for el in elements:
        gid = extract_id_from_urn(el.get("id", "")) or str(el.get("id", ""))
        groups.append({
            "id": gid,
            "name": el.get("name", ""),
            "status": el.get("status", ""),
        })

    return jsonify({"account_id": account_id, "groups": groups})


@app.route("/api/campaigns")
@requires_auth
def api_campaigns():
    """List campaigns from LinkedIn API, cross-referenced with schedules."""
    account_id = request.args.get("account_id", LINKEDIN_BUSINESS_ACCOUNT_ID)
    if not account_id:
        return jsonify({"error": "No account_id provided and LINKEDIN_BUSINESS_ACCOUNT_ID not set"}), 400

    filter_status = request.args.get("status")
    filter_group = request.args.get("campaign_group_id")

    try:
        elements = linkedin_paginated_request(
            f"/adAccounts/{account_id}/adCampaigns",
            params={"q": "search"},
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # Apply client-side filters
    if filter_status:
        elements = [el for el in elements if el.get("status") == filter_status]
    if filter_group:
        target_group_urn = f"urn:li:sponsoredCampaignGroup:{filter_group}"
        elements = [el for el in elements if el.get("campaignGroup") == target_group_urn]

    # Load schedules to cross-reference
    schedules = _load_schedules()
    schedule_map = {}
    for rule in schedules.get("weekday_only", []):
        schedule_map[rule["campaign_id"]] = rule

    campaigns = []
    for el in elements:
        cid = extract_id_from_urn(el.get("id", "")) or str(el.get("id", ""))
        budget = el.get("dailyBudget", {})
        schedule = schedule_map.get(cid)

        schedule_obj = {"enabled": False}
        if schedule:
            schedule_obj = {
                "enabled": True,
                "timezone": schedule["timezone"],
            }
            if "hours" in schedule:
                schedule_obj["hours"] = schedule["hours"]
            else:
                schedule_obj["resume_time"] = schedule.get("resume_time", "06:00")
                schedule_obj["pause_time"] = schedule.get("pause_time", "18:00")

        # Extract campaign group URN
        cg_urn = el.get("campaignGroup", "")
        cg_id = extract_id_from_urn(cg_urn) if cg_urn else ""

        # Bidding info
        unit_cost = el.get("unitCost", {})
        run_schedule = el.get("runSchedule", {})
        run_schedule_iso = {}
        if run_schedule.get("start"):
            run_schedule_iso["start"] = epoch_ms_to_iso(run_schedule["start"])
        if run_schedule.get("end"):
            run_schedule_iso["end"] = epoch_ms_to_iso(run_schedule["end"])

        campaigns.append({
            "id": cid,
            "name": el.get("name", ""),
            "status": el.get("status", ""),
            "dailyBudget": f"{budget.get('amount', 'N/A')} {budget.get('currencyCode', '')}".strip() if budget else "N/A",
            "dailyBudgetRaw": {"amount": budget.get("amount", ""), "currencyCode": budget.get("currencyCode", "")} if budget else None,
            "objectiveType": el.get("objectiveType", ""),
            "campaignGroup": cg_id,
            "costType": el.get("costType", ""),
            "bidStrategy": el.get("bidStrategy", ""),
            "unitCost": {"amount": unit_cost.get("amount", ""), "currencyCode": unit_cost.get("currencyCode", "")} if unit_cost else None,
            "pacingStrategy": el.get("pacingStrategy", ""),
            "runSchedule": run_schedule_iso if run_schedule_iso else None,
            "schedule": schedule_obj,
        })

    return jsonify({
        "account_id": account_id,
        "campaigns": campaigns,
    })


@app.route("/api/campaigns/<campaign_id>/status", methods=["POST"])
@requires_auth
def api_campaign_status(campaign_id):
    """Pause or resume a campaign."""
    account_id = request.json.get("account_id", LINKEDIN_BUSINESS_ACCOUNT_ID)
    status = request.json.get("status")

    if status not in ("ACTIVE", "PAUSED"):
        return jsonify({"error": "status must be ACTIVE or PAUSED"}), 400
    if not account_id:
        return jsonify({"error": "No account_id provided"}), 400

    body = {"patch": {"$set": {"status": status}}}
    extra_headers = {"X-RestLi-Method": "PARTIAL_UPDATE"}
    data = linkedin_api_request(
        "POST",
        f"/adAccounts/{account_id}/adCampaigns/{campaign_id}",
        json_body=body,
        extra_headers=extra_headers,
    )

    if "error" in data:
        return jsonify({"error": data["error"]}), 500

    return jsonify({"success": True, "campaign_id": campaign_id, "status": status})


@app.route("/api/campaigns/analytics")
@requires_auth
def api_campaigns_analytics():
    """Fetch 30-day average daily spend per campaign."""
    account_id = request.args.get("account_id", LINKEDIN_BUSINESS_ACCOUNT_ID)
    if not account_id:
        return jsonify({"error": "No account_id provided"}), 400

    today = datetime.now()
    start = today - timedelta(days=30)

    date_range = (
        f"(start:(year:{start.year},month:{start.month},day:{start.day}),"
        f"end:(year:{today.year},month:{today.month},day:{today.day}))"
    )
    # Build URL manually — LinkedIn requires RestLi syntax chars unencoded
    # but URN colons must be percent-encoded
    encoded_urn = f"urn%3Ali%3AsponsoredAccount%3A{account_id}"
    qs = (
        f"q=analytics&pivot=CAMPAIGN&timeGranularity=ALL"
        f"&accounts=List({encoded_urn})"
        f"&dateRange={date_range}"
        f"&fields=costInLocalCurrency,pivotValues,dateRange"
    )
    full_url = f"https://api.linkedin.com/rest/adAnalytics?{qs}"

    try:
        data = linkedin_api_request("GET", full_url)
        if "error" in data:
            raise RuntimeError(data["error"])
        elements = data.get("elements", [])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    result = {}
    for el in elements:
        # API returns pivotValues (array) or pivotValue (string)
        pivot_values = el.get("pivotValues", [])
        pivot_value = pivot_values[0] if pivot_values else el.get("pivotValue", "")
        cid = extract_id_from_urn(pivot_value) if pivot_value else ""
        if not cid:
            continue
        total_spend = float(el.get("costInLocalCurrency", 0))
        result[cid] = {
            "avgDailySpend": round(total_spend / 30, 2),
            "totalSpend": round(total_spend, 2),
        }

    return jsonify(result)


@app.route("/api/campaigns/<campaign_id>/update", methods=["POST"])
@requires_auth
def api_campaign_update(campaign_id):
    """Update campaign bidding, budget, and pacing settings."""
    payload = request.json or {}
    account_id = payload.get("account_id", LINKEDIN_BUSINESS_ACCOUNT_ID)
    if not account_id:
        return jsonify({"error": "No account_id provided"}), 400

    patch_set = {}

    bid_strategy = payload.get("bid_strategy")
    if bid_strategy:
        patch_set["bidStrategy"] = bid_strategy.upper()

    bid_amount = payload.get("bid_amount")
    bid_currency = payload.get("currency", "USD")
    if bid_amount is not None and bid_amount != "":
        patch_set["unitCost"] = {
            "amount": bid_amount,
            "currencyCode": bid_currency,
        }

    daily_budget = payload.get("daily_budget")
    if daily_budget is not None and daily_budget != "":
        patch_set["dailyBudget"] = {
            "amount": daily_budget,
            "currencyCode": bid_currency,
        }

    pacing_strategy = payload.get("pacing_strategy")
    if pacing_strategy:
        patch_set["pacingStrategy"] = pacing_strategy.upper()

    if not patch_set:
        return jsonify({"error": "No fields to update"}), 400

    body = {"patch": {"$set": patch_set}}
    extra_headers = {"X-RestLi-Method": "PARTIAL_UPDATE"}
    data = linkedin_api_request(
        "POST",
        f"/adAccounts/{account_id}/adCampaigns/{campaign_id}",
        json_body=body,
        extra_headers=extra_headers,
    )

    if "error" in data:
        return jsonify({"error": data["error"]}), 500

    return jsonify({"success": True, "campaign_id": campaign_id, "updated": list(patch_set.keys())})


@app.route("/api/schedules")
@requires_auth
def api_schedules():
    """Return all weekday schedule rules."""
    schedules = _load_schedules()
    return jsonify(schedules)


@app.route("/api/schedules", methods=["POST"])
@requires_auth
def api_add_schedule():
    """Add a campaign to weekday scheduling."""
    payload = request.json or {}
    required = ["campaign_id"]
    for field in required:
        if not payload.get(field):
            return jsonify({"error": f"Missing required field: {field}"}), 400

    campaign_id = str(payload["campaign_id"])
    account_id = str(payload.get("account_id", LINKEDIN_BUSINESS_ACCOUNT_ID))

    schedules = _load_schedules()

    # Check for duplicate
    for rule in schedules["weekday_only"]:
        if rule["campaign_id"] == campaign_id and rule["account_id"] == account_id:
            return jsonify({"error": f"Campaign {campaign_id} already has a weekday schedule"}), 409

    rule = {
        "account_id": account_id,
        "campaign_id": campaign_id,
        "campaign_name": payload.get("campaign_name", ""),
        "timezone": payload.get("timezone", "America/New_York"),
        "added_at": datetime.now().isoformat(timespec="seconds"),
    }

    if "hours" in payload:
        rule["hours"] = payload["hours"]
    else:
        rule["resume_time"] = payload.get("resume_time", "06:00")
        rule["pause_time"] = payload.get("pause_time", "18:00")

    schedules["weekday_only"].append(rule)
    _save_schedules(schedules)

    return jsonify({"success": True, "rule": rule}), 201


@app.route("/api/schedules/<campaign_id>", methods=["DELETE"])
@requires_auth
def api_delete_schedule(campaign_id):
    """Remove a campaign from weekday scheduling."""
    account_id = request.args.get("account_id", LINKEDIN_BUSINESS_ACCOUNT_ID)

    schedules = _load_schedules()
    original_count = len(schedules["weekday_only"])
    schedules["weekday_only"] = [
        r for r in schedules["weekday_only"]
        if not (r["campaign_id"] == str(campaign_id) and r["account_id"] == str(account_id))
    ]

    if len(schedules["weekday_only"]) == original_count:
        return jsonify({"error": f"Campaign {campaign_id} not found in schedules"}), 404

    _save_schedules(schedules)
    return jsonify({"success": True, "campaign_id": campaign_id})


@app.route("/api/scheduler/run", methods=["POST"])
@requires_auth
def api_run_scheduler():
    """Trigger the scheduler — evaluate all rules and pause/resume accordingly."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo

    schedules = _load_schedules()
    rules = schedules.get("weekday_only", [])

    if not rules:
        return jsonify({"message": "No weekday-only schedules configured.", "actions": []})

    DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

    actions = []
    for rule in rules:
        account_id = rule["account_id"]
        campaign_id = rule["campaign_id"]
        campaign_name = rule.get("campaign_name", campaign_id)
        tz_name = rule.get("timezone", "UTC")

        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            actions.append({
                "campaign_id": campaign_id,
                "campaign_name": campaign_name,
                "action": "SKIP",
                "reason": f"Invalid timezone '{tz_name}'",
            })
            continue

        now = datetime.now(tz)
        weekday = now.weekday()  # 0=Mon, 6=Sun
        current_time = now.strftime("%H:%M")

        if "hours" in rule:
            # New hours-grid model
            day_key = DAY_KEYS[weekday]
            active_hours = rule["hours"].get(day_key, [])
            desired = "ACTIVE" if now.hour in active_hours else "PAUSED"
        else:
            # Legacy resume_time/pause_time model
            resume_time_str = rule.get("resume_time", "06:00")
            pause_time_str = rule.get("pause_time", "18:00")
            if weekday >= 5:
                desired = "PAUSED"
            elif weekday == 4 and current_time >= pause_time_str:
                desired = "PAUSED"
            elif weekday == 0 and current_time < resume_time_str:
                desired = "PAUSED"
            else:
                desired = "ACTIVE"

        body = {"patch": {"$set": {"status": desired}}}
        extra_headers = {"X-RestLi-Method": "PARTIAL_UPDATE"}
        data = linkedin_api_request(
            "POST",
            f"/adAccounts/{account_id}/adCampaigns/{campaign_id}",
            json_body=body,
            extra_headers=extra_headers,
        )

        if "error" in data:
            actions.append({
                "campaign_id": campaign_id,
                "campaign_name": campaign_name,
                "action": "ERROR",
                "reason": str(data["error"]),
            })
        else:
            actions.append({
                "campaign_id": campaign_id,
                "campaign_name": campaign_name,
                "action": desired,
                "day": now.strftime("%A"),
                "time": current_time,
                "timezone": tz_name,
            })

    _scheduler_state["last_run"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    _record_run("manual", actions)

    return jsonify({
        "message": f"Processed {len(rules)} rule(s).",
        "actions": actions,
    })


# ---------------------------------------------------------------------------
# Background scheduler (runs every 30 minutes)
# ---------------------------------------------------------------------------

SCHEDULER_INTERVAL = int(os.environ.get("SCHEDULER_INTERVAL_MINUTES", 30)) * 60

logger = logging.getLogger("scheduler")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")

# Scheduler state — accessible via /api/scheduler/status
_scheduler_state = {
    "last_run": None,
    "next_run": None,
    "interval_minutes": SCHEDULER_INTERVAL // 60,
}

MAX_HISTORY = 50
HISTORY_FILE = os.path.join(
    "/data" if os.environ.get("RAILWAY_ENVIRONMENT") else os.path.dirname(os.path.abspath(__file__)),
    "scheduler_history.json",
)
MAX_WEEKLY_REPORTS = 12
WEEKLY_REPORTS_FILE = os.path.join(
    "/data" if os.environ.get("RAILWAY_ENVIRONMENT") else os.path.dirname(os.path.abspath(__file__)),
    "weekly_reports.json",
)


def _load_history():
    """Load run history from disk."""
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return []


def _save_history(history):
    """Persist run history to disk (capped at MAX_HISTORY)."""
    history = history[-MAX_HISTORY:]
    try:
        os.makedirs(os.path.dirname(HISTORY_FILE) or ".", exist_ok=True)
        with open(HISTORY_FILE, "w") as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        logger.warning("Could not save history: %s", e)


def _record_run(trigger, actions):
    """Append a run entry to history."""
    entry = {
        "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "trigger": trigger,
        "rules_processed": len(actions),
        "actions": actions,
    }
    history = _load_history()
    history.append(entry)
    _save_history(history)
    return entry


# ---------------------------------------------------------------------------
# Weekly report persistence
# ---------------------------------------------------------------------------

def _load_weekly_reports():
    """Load weekly reports from disk."""
    try:
        if os.path.exists(WEEKLY_REPORTS_FILE):
            with open(WEEKLY_REPORTS_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return []


def _save_weekly_reports(reports):
    """Persist weekly reports to disk (capped at MAX_WEEKLY_REPORTS)."""
    reports = reports[-MAX_WEEKLY_REPORTS:]
    try:
        os.makedirs(os.path.dirname(WEEKLY_REPORTS_FILE) or ".", exist_ok=True)
        with open(WEEKLY_REPORTS_FILE, "w") as f:
            json.dump(reports, f, indent=2)
    except Exception as e:
        logger.warning("Could not save weekly reports: %s", e)


# ---------------------------------------------------------------------------
# Weekly report generation
# ---------------------------------------------------------------------------

def _format_weekly_report(week_start, week_end, campaigns, analytics):
    """Format the weekly report as Slack-friendly bullet points."""
    start_str = f"{week_start.strftime('%b')} {week_start.day}"
    end_str = f"{week_end.strftime('%b')} {week_end.day}, {week_end.year}"

    lines = []
    lines.append("*InnoVint LinkedIn Ads -- Weekly Performance Summary*")
    lines.append(f"_Week of {start_str} - {end_str}_")
    lines.append("")

    # Calculate totals
    total_spend = sum(a["spend"] for a in analytics.values())
    total_impressions = sum(a["impressions"] for a in analytics.values())
    total_clicks = sum(a["clicks"] for a in analytics.values())
    total_leads = sum(a["leads"] for a in analytics.values())
    total_conversions = sum(a["conversions"] for a in analytics.values())
    ctr = (total_clicks / total_impressions * 100) if total_impressions > 0 else 0
    avg_cpc = (total_spend / total_clicks) if total_clicks > 0 else 0

    lines.append("*Totals*")
    lines.append(f"- Total Spend: ${total_spend:,.2f}")
    lines.append(f"- Impressions: {total_impressions:,}")
    lines.append(f"- Clicks: {total_clicks:,}")
    lines.append(f"- CTR: {ctr:.2f}%")
    lines.append(f"- Avg CPC: ${avg_cpc:.2f}")
    lines.append(f"- Leads: {total_leads:,}")
    lines.append(f"- Conversions: {total_conversions:,}")
    lines.append("")

    # Campaign breakdown
    if campaigns:
        lines.append("*Campaign Breakdown*")
        lines.append("")
        for cid, name in campaigns.items():
            a = analytics.get(cid, {})
            spend = a.get("spend", 0)
            impr = a.get("impressions", 0)
            clicks = a.get("clicks", 0)
            leads = a.get("leads", 0)
            convs = a.get("conversions", 0)
            c_ctr = (clicks / impr * 100) if impr > 0 else 0
            c_cpc = (spend / clicks) if clicks > 0 else 0

            lines.append(f"*{name}*")
            parts = [
                f"Spend: ${spend:,.2f}",
                f"Impr: {impr:,}",
                f"Clicks: {clicks:,}",
                f"CTR: {c_ctr:.2f}%",
                f"CPC: ${c_cpc:.2f}",
            ]
            if leads > 0:
                parts.append(f"{leads} lead{'s' if leads != 1 else ''}")
            if convs > 0:
                parts.append(f"{convs} conversion{'s' if convs != 1 else ''}")
            lines.append(" | ".join(parts))
            lines.append("")

    # Weekend pausing note
    lines.append("*Weekend Pausing*")
    lines.append("We are pausing LinkedIn ads on weekends to conserve budget and focus spend on weekdays when engagement is higher.")

    return "\n".join(lines)


def _generate_weekly_report(account_id=None):
    """Generate the weekly LinkedIn ads performance report."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo

    account_id = account_id or WEEKLY_REPORT_ACCOUNT_ID
    if not account_id:
        raise ValueError("No account ID configured for weekly report")

    tz = ZoneInfo(WEEKLY_REPORT_TIMEZONE)
    now = datetime.now(tz)

    # Calculate previous Mon-Sun
    this_monday = now.date() - timedelta(days=now.weekday())
    last_monday = this_monday - timedelta(days=7)
    last_sunday = this_monday - timedelta(days=1)

    # Fetch ACTIVE campaigns
    elements = linkedin_paginated_request(
        f"/adAccounts/{account_id}/adCampaigns",
        params={"q": "search"},
    )
    active_campaigns = {}
    for el in elements:
        if el.get("status") == "ACTIVE":
            cid = extract_id_from_urn(el.get("id", "")) or str(el.get("id", ""))
            active_campaigns[cid] = el.get("name", f"Campaign {cid}")
    logger.info("Weekly report: account=%s, %d active campaigns: %s", account_id, len(active_campaigns), list(active_campaigns.keys()))

    if not active_campaigns:
        report_text = _format_weekly_report(last_monday, last_sunday, {}, {})
        report_entry = {
            "generated_at": now.isoformat(timespec="seconds"),
            "week_start": last_monday.isoformat(),
            "week_end": last_sunday.isoformat(),
            "account_id": account_id,
            "report_text": report_text,
        }
        reports = _load_weekly_reports()
        reports.append(report_entry)
        _save_weekly_reports(reports)
        return report_entry

    # Fetch analytics for the week (end date is inclusive)
    date_range = (
        f"(start:(year:{last_monday.year},month:{last_monday.month},day:{last_monday.day}),"
        f"end:(year:{last_sunday.year},month:{last_sunday.month},day:{last_sunday.day}))"
    )
    encoded_urn = f"urn%3Ali%3AsponsoredAccount%3A{account_id}"
    qs = (
        f"q=analytics&pivot=CAMPAIGN&timeGranularity=ALL"
        f"&accounts=List({encoded_urn})"
        f"&dateRange={date_range}"
        f"&fields=costInLocalCurrency,impressions,clicks,oneClickLeads,externalWebsiteConversions,pivotValues,dateRange"
    )
    full_url = f"https://api.linkedin.com/rest/adAnalytics?{qs}"

    logger.info("Weekly report analytics URL: %s", full_url)
    data = linkedin_api_request("GET", full_url)
    logger.info("Weekly report analytics response keys: %s, elements count: %d", list(data.keys()), len(data.get("elements", [])))
    if "error" in data:
        raise RuntimeError(f"Analytics API error: {data['error']}")

    # Parse analytics per campaign (only active ones)
    campaign_analytics = {}
    for el in data.get("elements", []):
        pivot_values = el.get("pivotValues", [])
        pivot_value = pivot_values[0] if pivot_values else el.get("pivotValue", "")
        cid = extract_id_from_urn(pivot_value) if pivot_value else ""
        if not cid or cid not in active_campaigns:
            logger.debug("Weekly report: skipping analytics element pivot=%s cid=%s (not in active)", pivot_value, cid)
            continue
        campaign_analytics[cid] = {
            "spend": float(el.get("costInLocalCurrency", 0)),
            "impressions": int(el.get("impressions", 0)),
            "clicks": int(el.get("clicks", 0)),
            "leads": int(el.get("oneClickLeads", 0)),
            "conversions": int(el.get("externalWebsiteConversions", 0)),
        }

    report_text = _format_weekly_report(last_monday, last_sunday, active_campaigns, campaign_analytics)

    report_entry = {
        "generated_at": now.isoformat(timespec="seconds"),
        "week_start": last_monday.isoformat(),
        "week_end": last_sunday.isoformat(),
        "account_id": account_id,
        "report_text": report_text,
    }

    reports = _load_weekly_reports()
    reports.append(report_entry)
    _save_weekly_reports(reports)

    # Optionally post to Slack webhook
    if WEEKLY_REPORT_SLACK_WEBHOOK:
        try:
            requests_lib.post(
                WEEKLY_REPORT_SLACK_WEBHOOK,
                json={"text": report_text},
                timeout=10,
            )
            logger.info("Weekly report posted to Slack webhook.")
        except Exception as e:
            logger.warning("Failed to post weekly report to Slack: %s", e)

    return report_entry


def _check_weekly_report():
    """Check if it's time to auto-generate the weekly report (Monday 8:00-8:29 AM)."""
    if not WEEKLY_REPORT_ENABLED:
        return

    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo

    tz = ZoneInfo(WEEKLY_REPORT_TIMEZONE)
    now = datetime.now(tz)

    # Only on Monday between 8:00 and 8:29
    if now.weekday() != 0 or now.hour != 8 or now.minute >= 30:
        return

    # Idempotency: check last report date
    reports = _load_weekly_reports()
    if reports:
        last_generated = reports[-1].get("generated_at", "")
        if last_generated.startswith(now.date().isoformat()):
            return

    logger.info("Auto-generating weekly report...")
    try:
        _generate_weekly_report()
        logger.info("Weekly report auto-generated successfully.")
    except Exception as e:
        logger.exception("Failed to auto-generate weekly report: %s", e)


@app.route("/api/scheduler/status")
@requires_auth
def api_scheduler_status():
    """Return scheduler timing info."""
    return jsonify(_scheduler_state)


@app.route("/api/scheduler/history")
@requires_auth
def api_scheduler_history():
    """Return scheduler run history (most recent first)."""
    try:
        history = _load_history()
        history.reverse()
        return jsonify(history)
    except Exception as e:
        return jsonify([])


# ---------------------------------------------------------------------------
# Weekly report API routes
# ---------------------------------------------------------------------------

@app.route("/api/weekly-reports")
@requires_auth
def api_weekly_reports():
    """List all weekly reports (most recent first)."""
    reports = _load_weekly_reports()
    reports.reverse()
    return jsonify({"reports": reports})


@app.route("/api/weekly-reports/generate", methods=["POST"])
@requires_auth
def api_weekly_reports_generate():
    """Manually trigger weekly report generation."""
    try:
        account_id = (request.json or {}).get("account_id", WEEKLY_REPORT_ACCOUNT_ID)
        report = _generate_weekly_report(account_id)
        return jsonify({"success": True, "report": report})
    except Exception as e:
        logger.exception("Failed to generate weekly report: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/weekly-reports/latest")
@requires_auth
def api_weekly_reports_latest():
    """Get the most recent weekly report."""
    reports = _load_weekly_reports()
    if not reports:
        return jsonify({"error": "No reports generated yet"}), 404
    return jsonify({"report": reports[-1]})


def _run_scheduler_tick(trigger="auto"):
    """Execute one scheduler pass — evaluate all rules and pause/resume."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo

    schedules = _load_schedules()
    rules = schedules.get("weekday_only", [])
    if not rules:
        _record_run(trigger, [])
        return

    DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    actions = []

    for rule in rules:
        account_id = rule["account_id"]
        campaign_id = rule["campaign_id"]
        campaign_name = rule.get("campaign_name", campaign_id)
        tz_name = rule.get("timezone", "UTC")

        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            logger.warning("Invalid timezone '%s' for campaign %s", tz_name, campaign_id)
            actions.append({"campaign_name": campaign_name, "action": "SKIP", "reason": f"Invalid timezone '{tz_name}'"})
            continue

        now = datetime.now(tz)
        weekday = now.weekday()
        current_time = now.strftime("%H:%M")

        if "hours" in rule:
            day_key = DAY_KEYS[weekday]
            active_hours = rule["hours"].get(day_key, [])
            desired = "ACTIVE" if now.hour in active_hours else "PAUSED"
        else:
            resume_time_str = rule.get("resume_time", "06:00")
            pause_time_str = rule.get("pause_time", "18:00")
            if weekday >= 5:
                desired = "PAUSED"
            elif weekday == 4 and current_time >= pause_time_str:
                desired = "PAUSED"
            elif weekday == 0 and current_time < resume_time_str:
                desired = "PAUSED"
            else:
                desired = "ACTIVE"

        body = {"patch": {"$set": {"status": desired}}}
        extra_headers = {"X-RestLi-Method": "PARTIAL_UPDATE"}
        data = linkedin_api_request(
            "POST",
            f"/adAccounts/{account_id}/adCampaigns/{campaign_id}",
            json_body=body,
            extra_headers=extra_headers,
        )

        if "error" in data:
            logger.error("Failed %s for %s (%s): %s", desired, campaign_name, campaign_id, data["error"])
            actions.append({"campaign_name": campaign_name, "action": "ERROR", "reason": str(data["error"])})
        else:
            logger.info("%s -> %s (%s) [%s %s, tz=%s]", desired, campaign_name, campaign_id, now.strftime("%A"), current_time, tz_name)
            actions.append({"campaign_name": campaign_name, "action": desired, "day": now.strftime("%A"), "time": current_time, "timezone": tz_name})

    _record_run(trigger, actions)


def _update_next_run():
    """Set the next_run timestamp."""
    _scheduler_state["next_run"] = (
        datetime.utcnow() + timedelta(seconds=SCHEDULER_INTERVAL)
    ).strftime("%Y-%m-%d %H:%M:%S UTC")


def _scheduler_loop():
    """Background loop that runs the scheduler every SCHEDULER_INTERVAL seconds."""
    logger.info("Background scheduler started (interval=%d min)", SCHEDULER_INTERVAL // 60)
    _update_next_run()
    while True:
        time.sleep(SCHEDULER_INTERVAL)
        try:
            logger.info("Scheduler tick starting...")
            _scheduler_state["last_run"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            _run_scheduler_tick()
            _update_next_run()
            logger.info("Scheduler tick complete.")
        except Exception as e:
            logger.exception("Scheduler tick failed: %s", e)
            _update_next_run()
        # Check if weekly report needs generating
        _check_weekly_report()


def start_background_scheduler():
    """Start the scheduler in a daemon thread."""
    t = threading.Thread(target=_scheduler_loop, daemon=True)
    t.start()


# Start the background scheduler when the module is loaded (works with gunicorn)
start_background_scheduler()


# ---------------------------------------------------------------------------
# Bid Pacing — config, background daily runner, and API routes
# ---------------------------------------------------------------------------

BID_PACER_ENABLED = os.environ.get("BID_PACER_ENABLED", "true").lower() == "true"
BID_PACER_RUN_HOUR = int(os.environ.get("BID_PACER_RUN_HOUR", "9"))
BID_PACER_TIMEZONE = os.environ.get("BID_PACER_TIMEZONE", "America/New_York")
BID_PACER_CHECK_INTERVAL = int(os.environ.get("BID_PACER_CHECK_MINUTES", "15")) * 60

_bid_pacer_state = {
    "enabled": BID_PACER_ENABLED,
    "run_hour": BID_PACER_RUN_HOUR,
    "timezone": BID_PACER_TIMEZONE,
    "last_run": None,
    "last_run_date": None,
}


def _pacer_today_str():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(BID_PACER_TIMEZONE)).date().isoformat()
    except Exception:
        return datetime.utcnow().date().isoformat()


def _bid_pacer_loop():
    """Run the bid pacer once per day after BID_PACER_RUN_HOUR (in BID_PACER_TIMEZONE)."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo
    logger.info("Background bid pacer started (run_hour=%d %s, enabled=%s)",
                BID_PACER_RUN_HOUR, BID_PACER_TIMEZONE, BID_PACER_ENABLED)
    while True:
        try:
            if BID_PACER_ENABLED:
                now = datetime.now(ZoneInfo(BID_PACER_TIMEZONE))
                today = now.date().isoformat()
                if now.hour >= BID_PACER_RUN_HOUR and _bid_pacer_state["last_run_date"] != today:
                    if li._load_bid_pacing_rules().get("rules"):
                        logger.info("Bid pacer daily run starting...")
                        summary = li._run_bid_pacer_engine(dry_run=False)
                        logger.info("Bid pacer processed %d rule(s).", summary.get("rules_processed", 0))
                    _bid_pacer_state["last_run"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
                    _bid_pacer_state["last_run_date"] = today
        except Exception as e:
            logger.exception("Bid pacer loop failed: %s", e)
        time.sleep(BID_PACER_CHECK_INTERVAL)


def start_bid_pacer():
    """Start the daily bid pacer in a daemon thread."""
    t = threading.Thread(target=_bid_pacer_loop, daemon=True)
    t.start()


@app.route("/api/bid-pacing/rules")
@requires_auth
def api_bid_pacing_rules():
    """List all bid-pacing rules."""
    return jsonify(li._load_bid_pacing_rules().get("rules", []))


@app.route("/api/bid-pacing/rules", methods=["POST"])
@requires_auth
def api_bid_pacing_add():
    """Create or update a bid-pacing rule."""
    try:
        p = request.json or {}
        acct = str(p.get("account_id", "")).strip()
        cid = str(p.get("campaign_id", "")).strip()
        if not acct or not cid:
            return jsonify({"error": "account_id and campaign_id required"}), 400
        data = li._load_bid_pacing_rules()
        data["rules"] = [r for r in data["rules"]
                         if not (str(r["campaign_id"]) == cid and str(r["account_id"]) == acct)]
        data["rules"].append({
            "account_id": acct,
            "campaign_id": cid,
            "campaign_name": p.get("campaign_name", ""),
            "daily_budget": float(p.get("daily_budget") or 0),
            "monthly_account_cap": float(p.get("monthly_account_cap") or 0),
            "max_change_pct": float(p.get("max_change_pct") or 20),
            "min_bid": float(p.get("min_bid") or 0),
            "max_bid": float(p.get("max_bid") or 0),
            "min_change_pct": float(p.get("min_change_pct") or 2),
            "target_delivery_ratio": float(p.get("target_delivery_ratio") or 0.95),
            "max_cpl": float(p.get("max_cpl") or 0),
            "max_cpc": float(p.get("max_cpc") or 0),
            "efficiency_window_days": int(p.get("efficiency_window_days") or 7),
            "detect_ceiling": bool(p.get("detect_ceiling", True)),
            "timezone": p.get("timezone", "America/New_York"),
            "currency": p.get("currency", "USD"),
            "enabled": bool(p.get("enabled", True)),
            "added_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        })
        li._save_bid_pacing_rules(data)
        return jsonify({"success": True})
    except Exception as e:
        logger.exception("Failed to add bid-pacing rule: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/bid-pacing/rules/<campaign_id>", methods=["DELETE"])
@requires_auth
def api_bid_pacing_delete(campaign_id):
    """Remove a bid-pacing rule."""
    acct = request.args.get("account_id", "")
    data = li._load_bid_pacing_rules()
    before = len(data["rules"])
    data["rules"] = [r for r in data["rules"]
                     if not (str(r["campaign_id"]) == str(campaign_id) and (not acct or str(r["account_id"]) == str(acct)))]
    li._save_bid_pacing_rules(data)
    return jsonify({"success": True, "removed": before - len(data["rules"])})


@app.route("/api/bid-pacing/run", methods=["POST"])
@requires_auth
def api_bid_pacing_run():
    """Run the pacer now. Pass {"dry_run": true} to preview without applying."""
    try:
        dry = bool((request.json or {}).get("dry_run", False))
        summary = li._run_bid_pacer_engine(dry_run=dry)
        _bid_pacer_state["last_run"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        if not dry:
            _bid_pacer_state["last_run_date"] = _pacer_today_str()
        return jsonify(summary)
    except Exception as e:
        logger.exception("Bid pacer run failed: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/bid-pacing/history")
@requires_auth
def api_bid_pacing_history():
    """Return bid-change history, most recent first."""
    hist = sorted(li._load_bid_pacing_history(), key=lambda h: h.get("timestamp", ""), reverse=True)
    return jsonify(hist[:100])


@app.route("/api/bid-pacing/status")
@requires_auth
def api_bid_pacing_status():
    """Return bid pacer schedule/run state."""
    return jsonify(_bid_pacer_state)


@app.route("/api/bid-pacing/snapshot")
@requires_auth
def api_bid_pacing_snapshot():
    """Return a live pacing snapshot (bids, spend, cap projection) for an account."""
    acct = request.args.get("account_id", "")
    if not acct:
        return jsonify({"error": "account_id required"}), 400
    try:
        return jsonify(li._build_pacing_snapshot(acct))
    except Exception as e:
        logger.exception("Pacing snapshot failed: %s", e)
        return jsonify({"error": str(e)}), 500


# Start the background bid pacer when the module is loaded (works with gunicorn)
start_bid_pacer()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------



@app.route("/health")
def health():
    """Verify env vars + token validity + granted scopes (for Railway sanity check)."""
    keys = [
        "LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET", "LINKEDIN_ACCESS_TOKEN",
        "LINKEDIN_REFRESH_TOKEN", "LINKEDIN_ORG_URN", "LINKEDIN_API_VERSION",
        "LINKEDIN_OAUTH_SCOPES", "LINKEDIN_BUSINESS_ACCOUNT_ID",
    ]
    env_present = {k: bool(os.environ.get(k)) for k in keys}
    result = {"env_present": env_present}

    try:
        token = li.get_credentials()
    except Exception as e:
        result["token"] = f"ERROR: {e}"
        result["READY"] = False
        return jsonify(result), 500

    # Introspect the token to read its real granted scopes
    try:
        ir = requests_lib.post(
            "https://www.linkedin.com/oauth/v2/introspectToken",
            data={
                "client_id": os.environ.get("LINKEDIN_CLIENT_ID", ""),
                "client_secret": os.environ.get("LINKEDIN_CLIENT_SECRET", ""),
                "token": token,
            },
            timeout=20,
        )
        ij = ir.json()
        scopes = ij.get("scope", "")
        result["token_active"] = ij.get("active")
        result["scopes"] = scopes
        result["has_w_organization_social"] = "w_organization_social" in scopes
        result["has_rw_ads"] = "rw_ads" in scopes
    except Exception as e:
        result["introspect_error"] = str(e)
        scopes = ""

    # Quick API reachability check
    try:
        r = requests_lib.get(
            "https://api.linkedin.com/rest/adAccounts",
            headers=li.get_headers(token), params={"q": "search"}, timeout=20,
        )
        result["adAccounts_status"] = r.status_code
    except Exception as e:
        result["adAccounts_error"] = str(e)

    required = ["LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET", "LINKEDIN_ACCESS_TOKEN",
                "LINKEDIN_REFRESH_TOKEN", "LINKEDIN_ORG_URN"]
    result["READY"] = bool(all(env_present[k] for k in required)
                           and result.get("has_w_organization_social")
                           and result.get("has_rw_ads"))
    return jsonify(result)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=True)
