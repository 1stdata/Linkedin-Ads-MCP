"""
Flask dashboard for LinkedIn Ads weekday scheduling.

Provides a web UI to view campaigns, toggle weekday scheduling,
pause/resume campaigns, and trigger the scheduler manually.

Reuses API helpers from linkedin_ads_server.py — no duplicated logic.
"""

import os
import sys
from datetime import datetime, timedelta
from functools import wraps

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

    return jsonify({
        "message": f"Processed {len(rules)} rule(s).",
        "actions": actions,
    })


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=True)
