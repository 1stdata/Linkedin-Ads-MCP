from typing import Any, Dict, List, Optional, Union
from pydantic import Field
import os
import json
import requests
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode, quote
import logging

# MCP
from mcp.server.fastmcp import FastMCP

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('linkedin_ads_server')

mcp = FastMCP(
    "linkedin-ads-server",
    dependencies=[
        "requests",
        "python-dotenv",
        "mcp"
    ]
)

# Constants
LINKEDIN_API_BASE = "https://api.linkedin.com/rest"
LINKEDIN_OAUTH_TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
DEFAULT_API_VERSION = "202605"
DEFAULT_PAGE_SIZE = 50

# Load environment variables
try:
    from dotenv import load_dotenv
    load_dotenv()
    logger.info("Environment variables loaded from .env file")
except ImportError:
    logger.warning("python-dotenv not installed, skipping .env file loading")

# Get credentials from environment variables
LINKEDIN_CLIENT_ID = os.environ.get("LINKEDIN_CLIENT_ID", "")
LINKEDIN_CLIENT_SECRET = os.environ.get("LINKEDIN_CLIENT_SECRET", "")
LINKEDIN_BUSINESS_ACCOUNT_ID = os.environ.get("LINKEDIN_BUSINESS_ACCOUNT_ID", "")
LINKEDIN_ACCESS_TOKEN = os.environ.get("LINKEDIN_ACCESS_TOKEN", "")
LINKEDIN_REFRESH_TOKEN = os.environ.get("LINKEDIN_REFRESH_TOKEN", "")
LINKEDIN_API_VERSION = os.environ.get("LINKEDIN_API_VERSION", DEFAULT_API_VERSION)
LINKEDIN_TOKEN_PATH = os.environ.get("LINKEDIN_TOKEN_PATH", "")

# ---------------------------------------------------------------------------
# URN helpers
# ---------------------------------------------------------------------------

def format_account_urn(account_id: str) -> str:
    """Format an account ID into a LinkedIn sponsored account URN."""
    account_id = str(account_id).strip()
    if account_id.startswith("urn:"):
        return account_id
    return f"urn:li:sponsoredAccount:{account_id}"


def format_campaign_group_urn(account_id: str, group_id: str) -> str:
    """Format a campaign group URN."""
    group_id = str(group_id).strip()
    if group_id.startswith("urn:"):
        return group_id
    return f"urn:li:sponsoredCampaignGroup:{group_id}"


def format_campaign_urn(campaign_id: str) -> str:
    """Format a campaign URN."""
    campaign_id = str(campaign_id).strip()
    if campaign_id.startswith("urn:"):
        return campaign_id
    return f"urn:li:sponsoredCampaign:{campaign_id}"


def format_creative_urn(creative_id: str) -> str:
    """Format a creative URN."""
    creative_id = str(creative_id).strip()
    if creative_id.startswith("urn:"):
        return creative_id
    return f"urn:li:sponsoredCreative:{creative_id}"


def extract_id_from_urn(urn: str) -> str:
    """Extract the numeric ID from a LinkedIn URN."""
    if not urn:
        return ""
    return str(urn).split(":")[-1]

# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def iso_to_epoch_ms(iso_date: str) -> int:
    """Convert an ISO date string (YYYY-MM-DD) to epoch milliseconds."""
    dt = datetime.strptime(iso_date, "%Y-%m-%d")
    return int(dt.timestamp() * 1000)


def epoch_ms_to_iso(epoch_ms: int) -> str:
    """Convert epoch milliseconds to ISO date string."""
    if not epoch_ms:
        return "N/A"
    dt = datetime.fromtimestamp(epoch_ms / 1000)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def parse_date_params(start_date: str, end_date: str) -> dict:
    """Parse start/end date strings into LinkedIn analytics date range params.

    Returns a dict with a __raw_query key containing the Restli-encoded dateRange
    that must be appended to the URL without URL-encoding.
    """
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    raw = f"dateRange=(start:(year:{start.year},month:{start.month},day:{start.day}),end:(year:{end.year},month:{end.month},day:{end.day}))"
    return {"__raw_query": raw}


def _targeting_to_restli(obj) -> str:
    """Convert a targetingCriteria dict/list into a RestLi-2.0 query string.

    e.g. {"include":{"and":[{"or":{"urn:li:adTargetingFacet:locations":["urn:li:geo:1"]}}]}}
    -> (include:(and:List((or:(urn%3Ali%3AadTargetingFacet%3Alocations:List(urn%3Ali%3Ageo%3A1))))))

    URN keys/values are percent-encoded (safe="") so their own :(), characters
    are not confused with the RestLi structural tokens. Structural tokens
    ( ) : , List( are emitted literally. The result is appended to the URL via
    __raw_query so requests does not re-encode the structure.
    """
    from urllib.parse import quote
    if isinstance(obj, dict):
        return "(" + ",".join(f"{quote(str(k), safe='')}:{_targeting_to_restli(v)}"
                              for k, v in obj.items()) + ")"
    if isinstance(obj, list):
        return "List(" + ",".join(_targeting_to_restli(x) for x in obj) + ")"
    return quote(str(obj), safe="")

# ---------------------------------------------------------------------------
# Auth layer
# ---------------------------------------------------------------------------

def _load_token_from_file() -> Optional[dict]:
    """Load token data from the file specified by LINKEDIN_TOKEN_PATH."""
    if not LINKEDIN_TOKEN_PATH:
        return None
    if not os.path.exists(LINKEDIN_TOKEN_PATH):
        return None
    try:
        with open(LINKEDIN_TOKEN_PATH, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"Could not read token file {LINKEDIN_TOKEN_PATH}: {e}")
        return None


def _save_token_to_file(token_data: dict) -> None:
    """Persist token data to LINKEDIN_TOKEN_PATH."""
    if not LINKEDIN_TOKEN_PATH:
        return
    try:
        os.makedirs(os.path.dirname(LINKEDIN_TOKEN_PATH) or ".", exist_ok=True)
        with open(LINKEDIN_TOKEN_PATH, "w") as f:
            json.dump(token_data, f, indent=2)
        logger.info(f"Token saved to {LINKEDIN_TOKEN_PATH}")
    except IOError as e:
        logger.warning(f"Could not save token file: {e}")


def refresh_access_token(refresh_token: str) -> dict:
    """Refresh the LinkedIn access token using a refresh token.

    Returns:
        dict with keys: access_token, expires_in, refresh_token (if rotated)
    """
    if not LINKEDIN_CLIENT_ID or not LINKEDIN_CLIENT_SECRET:
        raise ValueError("LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET must be set to refresh tokens")

    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": LINKEDIN_CLIENT_ID,
        "client_secret": LINKEDIN_CLIENT_SECRET,
    }
    resp = requests.post(LINKEDIN_OAUTH_TOKEN_URL, data=payload)
    if resp.status_code != 200:
        raise RuntimeError(f"Token refresh failed ({resp.status_code}): {resp.text}")
    return resp.json()


def get_credentials() -> str:
    """Return a valid LinkedIn access token.

    Resolution order:
    1. Token file (LINKEDIN_TOKEN_PATH) — if it contains expires_at and is still valid, use it.
       If expired but a refresh_token is present, auto-refresh.
    2. LINKEDIN_ACCESS_TOKEN environment variable.
    3. Raise an error.
    """
    # 1. Try token file
    token_data = _load_token_from_file()
    if token_data:
        access_token = token_data.get("access_token", "")
        expires_at = token_data.get("expires_at", 0)
        if access_token and (expires_at == 0 or time.time() < expires_at - 60):
            return access_token
        # Token expired — try refresh
        rt = token_data.get("refresh_token") or LINKEDIN_REFRESH_TOKEN
        if rt:
            logger.info("Access token expired, refreshing...")
            new_data = refresh_access_token(rt)
            new_data["expires_at"] = time.time() + new_data.get("expires_in", 5184000)
            if "refresh_token" not in new_data:
                new_data["refresh_token"] = rt
            _save_token_to_file(new_data)
            return new_data["access_token"]

    # 2. Environment variable
    if LINKEDIN_ACCESS_TOKEN:
        return LINKEDIN_ACCESS_TOKEN

    # 3. Try refresh from env var
    if LINKEDIN_REFRESH_TOKEN:
        logger.info("No access token found, attempting refresh from env refresh token...")
        new_data = refresh_access_token(LINKEDIN_REFRESH_TOKEN)
        new_data["expires_at"] = time.time() + new_data.get("expires_in", 5184000)
        if "refresh_token" not in new_data:
            new_data["refresh_token"] = LINKEDIN_REFRESH_TOKEN
        _save_token_to_file(new_data)
        return new_data["access_token"]

    raise ValueError(
        "No LinkedIn access token available. Set LINKEDIN_ACCESS_TOKEN or "
        "LINKEDIN_REFRESH_TOKEN in your environment, or provide a token file via LINKEDIN_TOKEN_PATH."
    )


def get_headers(access_token: Optional[str] = None) -> dict:
    """Build standard LinkedIn Marketing API headers."""
    token = access_token or get_credentials()
    return {
        "Authorization": f"Bearer {token}",
        "Linkedin-Version": LINKEDIN_API_VERSION,
        "X-Restli-Protocol-Version": "2.0.0",
        "Content-Type": "application/json",
    }

# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def linkedin_api_request(
    method: str,
    path: str,
    params: Optional[dict] = None,
    json_body: Optional[dict] = None,
    extra_headers: Optional[dict] = None,
    access_token: Optional[str] = None,
) -> dict:
    """Centralized LinkedIn API caller with 401 retry.

    Args:
        method: HTTP method (GET, POST, DELETE)
        path: Path relative to LINKEDIN_API_BASE (e.g. "/adAccounts")
        params: Query parameters
        json_body: JSON body for POST/PUT
        extra_headers: Additional headers to merge
        access_token: Override access token

    Returns:
        Parsed JSON response dict, or {"status_code": N, "error": ...} on failure.
    """
    url = f"{LINKEDIN_API_BASE}{path}" if not path.startswith("http") else path
    headers = get_headers(access_token)
    if extra_headers:
        headers.update(extra_headers)

    # Handle raw query params that must not be URL-encoded (Restli format)
    # Use pop on a copy so the caller's dict retains __raw_query for pagination
    raw_query = ""
    if params and "__raw_query" in params:
        params = dict(params)
        raw_query = params.pop("__raw_query")

    if raw_query:
        # Build URL manually: let requests encode the normal params first via PreparedRequest
        req = requests.Request(method, url, headers=headers, params=params, json=json_body)
        prepared = req.prepare()
        separator = "&" if "?" in prepared.url else "?"
        prepared.url = f"{prepared.url}{separator}{raw_query}"
        resp = requests.Session().send(prepared)
    else:
        resp = requests.request(method, url, headers=headers, params=params, json=json_body)

    # Auto-retry on 401 (token may have just expired)
    if resp.status_code == 401:
        logger.info("Received 401, attempting token refresh...")
        try:
            new_token = get_credentials()
            headers["Authorization"] = f"Bearer {new_token}"
            if raw_query:
                req = requests.Request(method, url, headers=headers, params=params, json=json_body)
                prepared = req.prepare()
                separator = "&" if "?" in prepared.url else "?"
                prepared.url = f"{prepared.url}{separator}{raw_query}"
                resp = requests.Session().send(prepared)
            else:
                resp = requests.request(method, url, headers=headers, params=params, json=json_body)
        except Exception as e:
            logger.error(f"Token refresh failed: {e}")

    if resp.status_code >= 400:
        return {"status_code": resp.status_code, "error": resp.text}

    # Some LinkedIn endpoints return 201/204 with no body
    if resp.status_code in (204,):
        return {"status_code": resp.status_code, "success": True}
    if resp.status_code == 201:
        # Check for Location header (new resource ID)
        location = resp.headers.get("X-RestLi-Id", resp.headers.get("x-restli-id", ""))
        try:
            body = resp.json()
        except Exception:
            body = {}
        body["_created_id"] = location
        body["status_code"] = 201
        return body

    try:
        return resp.json()
    except Exception:
        return {"status_code": resp.status_code, "body": resp.text}


def linkedin_paginated_request(
    path: str,
    params: Optional[dict] = None,
    max_results: int = 200,
    access_token: Optional[str] = None,
    extra_headers: Optional[dict] = None,
) -> list:
    """Handle LinkedIn start/count pagination and return aggregated elements."""
    params = dict(params or {})
    page_size = min(max_results, DEFAULT_PAGE_SIZE)
    params["count"] = page_size
    params["start"] = 0
    all_elements: list = []

    while len(all_elements) < max_results:
        data = linkedin_api_request("GET", path, params=params, access_token=access_token, extra_headers=extra_headers)
        if "error" in data:
            raise RuntimeError(f"API error: {data['error']}")
        elements = data.get("elements", [])
        if not elements:
            break
        all_elements.extend(elements)
        if len(elements) < page_size:
            break
        params["start"] += page_size

    return all_elements[:max_results]

# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def format_output(data: Any, format_type: str = "table", fields: Optional[List[str]] = None) -> str:
    """Format data as table, JSON, or CSV.

    Args:
        data: A list of dicts (rows) to format.
        format_type: "table", "json", or "csv"
        fields: Ordered list of field keys. If None, derived from first row.
    """
    if not data:
        return "No data to display."

    if isinstance(data, dict):
        data = [data]

    if format_type.lower() == "json":
        return json.dumps(data, indent=2, default=str)

    # Derive fields from first row if not specified
    if not fields:
        fields = list(data[0].keys())

    if format_type.lower() == "csv":
        csv_lines = [",".join(fields)]
        for row in data:
            values = [str(row.get(f, "")).replace(",", ";") for f in fields]
            csv_lines.append(",".join(values))
        return "\n".join(csv_lines)

    # Default: table format
    # Calculate column widths
    col_widths: Dict[str, int] = {f: len(f) for f in fields}
    for row in data:
        for f in fields:
            val = str(row.get(f, ""))
            col_widths[f] = max(col_widths[f], len(val))

    header = " | ".join(f"{f:{col_widths[f]}}" for f in fields)
    separator = "-" * len(header)
    lines = [header, separator]
    for row in data:
        row_str = " | ".join(f"{str(row.get(f, '')):{col_widths[f]}}" for f in fields)
        lines.append(row_str)
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# A. Account Management (2 tools)
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_accounts(
    format: str = Field(default="table", description="Output format: 'table', 'json', or 'csv'"),
) -> str:
    """
    List all LinkedIn Ad Accounts accessible to the authenticated user.

    RECOMMENDED WORKFLOW:
    1. Run this tool first to discover available ad account IDs.
    2. Use the returned account ID in subsequent tools.

    Returns:
        A formatted list of ad accounts with ID, name, status, and currency.
    """
    try:
        params = {
            "q": "search",
            "__raw_query": "search=(status:(values:List(ACTIVE,DRAFT,CANCELED)))",
        }
        elements = linkedin_paginated_request("/adAccounts", params=params)

        if not elements:
            return "No accessible LinkedIn Ad Accounts found."

        rows = []
        for acct in elements:
            rows.append({
                "id": extract_id_from_urn(acct.get("id", acct.get("reference", ""))),
                "name": acct.get("name", "N/A"),
                "status": acct.get("status", "N/A"),
                "type": acct.get("type", "N/A"),
                "currency": acct.get("currency", "N/A"),
                "notifiedOnCreativeApproval": acct.get("notifiedOnCreativeApproval", "N/A"),
            })

        output_lines = ["LinkedIn Ad Accounts:"]
        output_lines.append("=" * 80)
        output_lines.append(format_output(rows, format_type=format,
                                          fields=["id", "name", "status", "type", "currency"]))
        return "\n".join(output_lines)
    except Exception as e:
        return f"Error listing accounts: {str(e)}"


@mcp.tool()
async def get_account_details(
    account_id: str = Field(description="LinkedIn Ad Account ID (numeric, e.g. '511389977')"),
) -> str:
    """
    Get detailed information about a specific LinkedIn Ad Account.

    Args:
        account_id: The numeric ad account ID.

    Returns:
        Account details including currency, status, type, and total budget.
    """
    try:
        data = linkedin_api_request("GET", f"/adAccounts/{account_id}")
        if "error" in data:
            return f"Error: {data['error']}"

        lines = [f"Account Details for {account_id}:"]
        lines.append("=" * 60)
        for key in ["name", "status", "type", "currency", "totalBudget",
                     "notifiedOnCreativeApproval", "notifiedOnEndOfCampaign",
                     "servingStatuses", "reference"]:
            val = data.get(key, "N/A")
            if isinstance(val, dict):
                val = json.dumps(val)
            lines.append(f"  {key}: {val}")

        return "\n".join(lines)
    except Exception as e:
        return f"Error getting account details: {str(e)}"

# ---------------------------------------------------------------------------
# B. Campaign Group Management (3 tools)
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_campaign_groups(
    account_id: str = Field(description="LinkedIn Ad Account ID"),
    status_filter: str = Field(default="", description="Filter by status: ACTIVE, PAUSED, ARCHIVED, DRAFT, CANCELED (comma-separated for multiple)"),
    format: str = Field(default="table", description="Output format: 'table', 'json', or 'csv'"),
) -> str:
    """
    List campaign groups for a LinkedIn Ad Account.

    Args:
        account_id: The numeric ad account ID.
        status_filter: Optional comma-separated statuses to filter by.
        format: Output format.

    Returns:
        A formatted list of campaign groups.
    """
    try:
        # Account goes in the PATH; the finder rejects `account` in the search
        # criteria (400 FIELD_INVALID "search/account"). Only status in search.
        params: dict = {"q": "search"}
        if status_filter:
            statuses = ",".join(s.strip() for s in status_filter.split(","))
            params["__raw_query"] = f"search=(status:(values:List({statuses})))"

        elements = linkedin_paginated_request(f"/adAccounts/{account_id}/adCampaignGroups", params=params)

        if not elements:
            return "No campaign groups found."

        rows = []
        for grp in elements:
            schedule = grp.get("runSchedule", {})
            rows.append({
                "id": extract_id_from_urn(grp.get("id", "")),
                "name": grp.get("name", "N/A"),
                "status": grp.get("status", "N/A"),
                "totalBudget": grp.get("totalBudget", {}).get("amount", "N/A"),
                "currency": grp.get("totalBudget", {}).get("currencyCode", "N/A"),
                "startDate": epoch_ms_to_iso(schedule.get("start", 0)),
                "endDate": epoch_ms_to_iso(schedule.get("end", 0)),
            })

        output_lines = [f"Campaign Groups for Account {account_id}:"]
        output_lines.append("=" * 80)
        output_lines.append(format_output(rows, format_type=format))
        return "\n".join(output_lines)
    except Exception as e:
        return f"Error listing campaign groups: {str(e)}"


@mcp.tool()
async def create_campaign_group(
    account_id: str = Field(description="LinkedIn Ad Account ID"),
    name: str = Field(description="Name for the campaign group"),
    status: str = Field(default="DRAFT", description="Initial status: ACTIVE, PAUSED, or DRAFT"),
    total_budget_amount: str = Field(default="", description="Total budget amount (e.g. '1000.00'). Leave empty for no cap."),
    total_budget_currency: str = Field(default="USD", description="Budget currency code (e.g. 'USD')"),
    start_date: str = Field(default="", description="Start date in YYYY-MM-DD format. Leave empty for immediate."),
    end_date: str = Field(default="", description="End date in YYYY-MM-DD format. Leave empty for no end."),
) -> str:
    """
    Create a new campaign group under a LinkedIn Ad Account.

    Args:
        account_id: The numeric ad account ID.
        name: Campaign group name.
        status: Initial status (ACTIVE, PAUSED, DRAFT).
        total_budget_amount: Optional total budget amount.
        total_budget_currency: Currency for budget.
        start_date: Optional start date (YYYY-MM-DD).
        end_date: Optional end date (YYYY-MM-DD).

    Returns:
        Confirmation with the new campaign group ID.
    """
    try:
        body: dict = {
            "account": format_account_urn(account_id),
            "name": name,
            "status": status.upper(),
        }

        if total_budget_amount:
            body["totalBudget"] = {
                "amount": total_budget_amount,
                "currencyCode": total_budget_currency,
            }

        run_schedule: dict = {}
        if start_date:
            run_schedule["start"] = iso_to_epoch_ms(start_date)
        if end_date:
            run_schedule["end"] = iso_to_epoch_ms(end_date)
        if run_schedule:
            body["runSchedule"] = run_schedule

        data = linkedin_api_request("POST", f"/adAccounts/{account_id}/adCampaignGroups", json_body=body)
        if "error" in data:
            return f"Error creating campaign group: {data['error']}"

        created_id = data.get("_created_id", "unknown")
        return f"Campaign group created successfully.\nID: {created_id}\nName: {name}\nStatus: {status}"
    except Exception as e:
        return f"Error creating campaign group: {str(e)}"


@mcp.tool()
async def update_campaign_group(
    account_id: str = Field(description="LinkedIn Ad Account ID"),
    group_id: str = Field(description="Campaign group ID to update"),
    name: str = Field(default="", description="New name (leave empty to keep current)"),
    status: str = Field(default="", description="New status: ACTIVE, PAUSED, ARCHIVED, CANCELED (leave empty to keep current)"),
    total_budget_amount: str = Field(default="", description="New total budget amount (leave empty to keep current)"),
    total_budget_currency: str = Field(default="USD", description="Budget currency code"),
    start_date: str = Field(default="", description="New start date YYYY-MM-DD (leave empty to keep current)"),
    end_date: str = Field(default="", description="New end date YYYY-MM-DD (leave empty to keep current)"),
) -> str:
    """
    Update an existing campaign group using partial update (PATCH semantics).

    Args:
        account_id: The numeric ad account ID.
        group_id: The campaign group ID to update.
        name: New name (optional).
        status: New status (optional).
        total_budget_amount: New budget amount (optional).
        total_budget_currency: Currency code for budget.
        start_date: New start date (optional).
        end_date: New end date (optional).

    Returns:
        Confirmation of the update.
    """
    try:
        patch_set: dict = {}
        if name:
            patch_set["name"] = name
        if status:
            patch_set["status"] = status.upper()
        if total_budget_amount:
            patch_set["totalBudget"] = {
                "amount": total_budget_amount,
                "currencyCode": total_budget_currency,
            }

        run_schedule: dict = {}
        if start_date:
            run_schedule["start"] = iso_to_epoch_ms(start_date)
        if end_date:
            run_schedule["end"] = iso_to_epoch_ms(end_date)
        if run_schedule:
            patch_set["runSchedule"] = run_schedule

        if not patch_set:
            return "No fields to update. Provide at least one field to change."

        body = {"patch": {"$set": patch_set}}
        extra_headers = {"X-RestLi-Method": "PARTIAL_UPDATE"}

        data = linkedin_api_request(
            "POST",
            f"/adAccounts/{account_id}/adCampaignGroups/{group_id}",
            json_body=body,
            extra_headers=extra_headers,
        )
        if "error" in data:
            return f"Error updating campaign group: {data['error']}"

        updated_fields = ", ".join(patch_set.keys())
        return f"Campaign group {group_id} updated successfully.\nUpdated fields: {updated_fields}"
    except Exception as e:
        return f"Error updating campaign group: {str(e)}"

# ---------------------------------------------------------------------------
# C. Campaign Management (5 tools)
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_campaigns(
    account_id: str = Field(description="LinkedIn Ad Account ID"),
    status_filter: str = Field(default="", description="Filter by status: ACTIVE, PAUSED, ARCHIVED, DRAFT, CANCELED (comma-separated)"),
    campaign_group_id: str = Field(default="", description="Filter by campaign group ID"),
    format: str = Field(default="table", description="Output format: 'table', 'json', or 'csv'"),
) -> str:
    """
    List campaigns for a LinkedIn Ad Account.

    Args:
        account_id: The numeric ad account ID.
        status_filter: Optional comma-separated statuses.
        campaign_group_id: Optional campaign group ID to filter by.
        format: Output format.

    Returns:
        A formatted list of campaigns with key details.
    """
    try:
        # Account goes in the PATH. Do NOT put `account` in the search criteria —
        # the /rest finder rejects it (400 FIELD_INVALID "search/account
        # unrecognized field"). Only status/campaignGroup belong in search.
        search_parts = []
        if status_filter:
            statuses = ",".join(s.strip() for s in status_filter.split(","))
            search_parts.append(f"status:(values:List({statuses}))")
        if campaign_group_id:
            grp_urn = format_campaign_group_urn(account_id, campaign_group_id).replace(":", "%3A")
            search_parts.append(f"campaignGroup:(values:List({grp_urn}))")

        params = {"q": "search"}
        if search_parts:
            params["__raw_query"] = "search=(" + ",".join(search_parts) + ")"

        elements = linkedin_paginated_request(f"/adAccounts/{account_id}/adCampaigns", params=params)

        if not elements:
            return "No campaigns found."

        rows = []
        for c in elements:
            schedule = c.get("runSchedule", {})
            daily = c.get("dailyBudget", {})
            rows.append({
                "id": extract_id_from_urn(c.get("id", "")),
                "name": c.get("name", "N/A"),
                "status": c.get("status", "N/A"),
                "type": c.get("type", "N/A"),
                "objective": c.get("objectiveType", "N/A"),
                "dailyBudget": daily.get("amount", "N/A"),
                "currency": daily.get("currencyCode", ""),
                "startDate": epoch_ms_to_iso(schedule.get("start", 0)),
                "endDate": epoch_ms_to_iso(schedule.get("end", 0)),
            })

        output_lines = [f"Campaigns for Account {account_id}:"]
        output_lines.append("=" * 100)
        output_lines.append(format_output(rows, format_type=format))
        return "\n".join(output_lines)
    except Exception as e:
        return f"Error listing campaigns: {str(e)}"


@mcp.tool()
async def get_campaign_details(
    account_id: str = Field(description="LinkedIn Ad Account ID"),
    campaign_id: str = Field(description="Campaign ID to retrieve"),
) -> str:
    """
    Get full configuration details for a specific campaign.

    Args:
        account_id: The numeric ad account ID.
        campaign_id: The campaign ID.

    Returns:
        Detailed campaign configuration dump.
    """
    try:
        data = linkedin_api_request("GET", f"/adAccounts/{account_id}/adCampaigns/{campaign_id}")
        if "error" in data:
            return f"Error: {data['error']}"

        lines = [f"Campaign Details — {campaign_id}:"]
        lines.append("=" * 60)

        key_fields = [
            "name", "status", "type", "objectiveType", "costType",
            "dailyBudget", "totalBudget", "unitCost", "bidStrategy",
            "pacingStrategy", "runSchedule", "campaignGroup",
            "audienceExpansionEnabled", "offsiteDeliveryEnabled",
            "targetingCriteria", "creativeSelection", "servingStatuses",
            "optimizationTargetType", "locale", "version",
        ]
        for key in key_fields:
            val = data.get(key, "N/A")
            if isinstance(val, (dict, list)):
                val = json.dumps(val, indent=2, default=str)
            lines.append(f"  {key}: {val}")

        return "\n".join(lines)
    except Exception as e:
        return f"Error getting campaign details: {str(e)}"


@mcp.tool()
async def create_campaign(
    account_id: str = Field(description="LinkedIn Ad Account ID"),
    name: str = Field(description="Campaign name"),
    campaign_group_id: str = Field(description="Campaign group ID this campaign belongs to"),
    objective_type: str = Field(description="Objective: BRAND_AWARENESS, WEBSITE_VISITS, ENGAGEMENT, VIDEO_VIEWS, LEAD_GENERATION, WEBSITE_CONVERSIONS, JOB_APPLICANTS, TALENT_LEADS"),
    campaign_type: str = Field(default="SPONSORED_UPDATES", description="Type: SPONSORED_UPDATES, SPONSORED_INMAILS, TEXT_ADS, DYNAMIC"),
    daily_budget_amount: str = Field(description="Daily budget amount (e.g. '50.00')"),
    daily_budget_currency: str = Field(default="USD", description="Currency code"),
    cost_type: str = Field(default="CPM", description="Cost type: CPC, CPM, CPV"),
    bid_strategy: str = Field(default="", description="Bid strategy: MAXIMUM_DELIVERY, TARGET_COST, or MANUAL_CPC (leave empty for default)"),
    bid_amount: str = Field(default="", description="Bid amount (e.g. '5.00'). Leave empty for auto-bid."),
    status: str = Field(default="DRAFT", description="Initial status: ACTIVE, PAUSED, DRAFT"),
    start_date: str = Field(default="", description="Start date YYYY-MM-DD (leave empty for immediate)"),
    end_date: str = Field(default="", description="End date YYYY-MM-DD (leave empty for no end)"),
    pacing_strategy: str = Field(default="", description="LIFETIME or DAILY pacing strategy"),
    locale_country: str = Field(default="US", description="Target locale country code"),
    locale_language: str = Field(default="en", description="Target locale language code"),
) -> str:
    """
    Create a new campaign under a LinkedIn Ad Account.

    Args:
        account_id: The numeric ad account ID.
        name: Campaign name.
        campaign_group_id: Parent campaign group ID.
        objective_type: Campaign objective.
        campaign_type: Campaign type.
        daily_budget_amount: Daily budget amount.
        daily_budget_currency: Currency code.
        cost_type: Cost/bid type.
        bid_strategy: Optional bid strategy (MAXIMUM_DELIVERY, TARGET_COST, MANUAL_CPC).
        bid_amount: Optional bid amount.
        status: Initial status.
        start_date: Optional start date.
        end_date: Optional end date.
        pacing_strategy: Optional pacing strategy.
        locale_country: Locale country.
        locale_language: Locale language.

    Returns:
        Confirmation with the new campaign ID.
    """
    try:
        body: dict = {
            "account": format_account_urn(account_id),
            "campaignGroup": format_campaign_group_urn(account_id, campaign_group_id),
            "offsiteDeliveryEnabled": False,
            "politicalIntent": "NOT_POLITICAL",
            "name": name,
            "objectiveType": objective_type.upper(),
            "type": campaign_type.upper(),
            "costType": cost_type.upper(),
            "status": status.upper(),
            "locale": {"country": locale_country, "language": locale_language},
            "dailyBudget": {
                "amount": daily_budget_amount,
                "currencyCode": daily_budget_currency,
            },
        }

        if bid_strategy:
            body["bidStrategy"] = bid_strategy.upper()

        if bid_amount:
            body["unitCost"] = {
                "amount": bid_amount,
                "currencyCode": daily_budget_currency,
            }

        if pacing_strategy:
            body["pacingStrategy"] = pacing_strategy.upper()

        run_schedule: dict = {}
        if start_date:
            run_schedule["start"] = iso_to_epoch_ms(start_date)
        if end_date:
            run_schedule["end"] = iso_to_epoch_ms(end_date)
        if run_schedule:
            body["runSchedule"] = run_schedule

        data = linkedin_api_request("POST", f"/adAccounts/{account_id}/adCampaigns", json_body=body)
        if "error" in data:
            return f"Error creating campaign: {data['error']}"

        created_id = data.get("_created_id", "unknown")
        return (
            f"Campaign created successfully.\n"
            f"ID: {created_id}\n"
            f"Name: {name}\n"
            f"Objective: {objective_type}\n"
            f"Status: {status}\n"
            f"Daily Budget: {daily_budget_amount} {daily_budget_currency}"
        )
    except Exception as e:
        return f"Error creating campaign: {str(e)}"


@mcp.tool()
async def update_campaign(
    account_id: str = Field(description="LinkedIn Ad Account ID"),
    campaign_id: str = Field(description="Campaign ID to update"),
    name: str = Field(default="", description="New name (leave empty to keep current)"),
    status: str = Field(default="", description="New status: ACTIVE, PAUSED, ARCHIVED, CANCELED"),
    daily_budget_amount: str = Field(default="", description="New daily budget amount"),
    daily_budget_currency: str = Field(default="USD", description="Currency code"),
    bid_strategy: str = Field(default="", description="New bid strategy: MAXIMUM_DELIVERY, TARGET_COST, or MANUAL_CPC"),
    bid_amount: str = Field(default="", description="New bid amount"),
    start_date: str = Field(default="", description="New start date YYYY-MM-DD"),
    end_date: str = Field(default="", description="New end date YYYY-MM-DD"),
    pacing_strategy: str = Field(default="", description="LIFETIME or DAILY pacing strategy"),
) -> str:
    """
    Update an existing campaign using partial update (PATCH semantics).

    Args:
        account_id: The numeric ad account ID.
        campaign_id: The campaign ID to update.
        name: New name (optional).
        status: New status (optional).
        daily_budget_amount: New daily budget (optional).
        daily_budget_currency: Currency code.
        bid_strategy: New bid strategy (optional).
        bid_amount: New bid amount (optional).
        start_date: New start date (optional).
        end_date: New end date (optional).
        pacing_strategy: New pacing strategy (optional).

    Returns:
        Confirmation of the update.
    """
    try:
        patch_set: dict = {}
        if name:
            patch_set["name"] = name
        if status:
            patch_set["status"] = status.upper()
        if daily_budget_amount:
            patch_set["dailyBudget"] = {
                "amount": daily_budget_amount,
                "currencyCode": daily_budget_currency,
            }
        if bid_strategy:
            patch_set["bidStrategy"] = bid_strategy.upper()
        if bid_amount:
            patch_set["unitCost"] = {
                "amount": bid_amount,
                "currencyCode": daily_budget_currency,
            }
        if pacing_strategy:
            patch_set["pacingStrategy"] = pacing_strategy.upper()

        run_schedule: dict = {}
        if start_date:
            run_schedule["start"] = iso_to_epoch_ms(start_date)
        if end_date:
            run_schedule["end"] = iso_to_epoch_ms(end_date)
        if run_schedule:
            patch_set["runSchedule"] = run_schedule

        if not patch_set:
            return "No fields to update. Provide at least one field to change."

        body = {"patch": {"$set": patch_set}}
        extra_headers = {"X-RestLi-Method": "PARTIAL_UPDATE"}

        data = linkedin_api_request(
            "POST",
            f"/adAccounts/{account_id}/adCampaigns/{campaign_id}",
            json_body=body,
            extra_headers=extra_headers,
        )
        if "error" in data:
            return f"Error updating campaign: {data['error']}"

        updated_fields = ", ".join(patch_set.keys())
        return f"Campaign {campaign_id} updated successfully.\nUpdated fields: {updated_fields}"
    except Exception as e:
        return f"Error updating campaign: {str(e)}"


@mcp.tool()
async def set_bid_strategy(
    account_id: str = Field(description="LinkedIn Ad Account ID"),
    campaign_id: str = Field(description="Campaign ID to update"),
    bid_strategy: str = Field(description="Bid strategy: MAXIMUM_DELIVERY, TARGET_COST, or MANUAL_CPC"),
    bid_amount: str = Field(default="", description="Bid amount (e.g. '5.00'). Required for TARGET_COST and MANUAL_CPC."),
    bid_currency: str = Field(default="USD", description="Currency code for bid amount"),
) -> str:
    """
    Convenience tool to update only the bid strategy and optional bid amount on a campaign.

    LinkedIn supports three bid strategies:
    - MAXIMUM_DELIVERY: Automatically optimizes bids to spend full budget (no bid amount needed)
    - TARGET_COST: Aims for a target cost per result (bid amount = target cost)
    - MANUAL_CPC: Manual cost-per-click bidding (bid amount = max CPC)

    Args:
        account_id: The numeric ad account ID.
        campaign_id: The campaign ID to update.
        bid_strategy: The bid strategy to set.
        bid_amount: Optional bid amount (required for TARGET_COST and MANUAL_CPC).
        bid_currency: Currency code for the bid amount.

    Returns:
        Confirmation of the bid strategy update.
    """
    try:
        strategy = bid_strategy.upper()
        valid_strategies = {"MAXIMUM_DELIVERY", "TARGET_COST", "MANUAL_CPC"}
        if strategy not in valid_strategies:
            return f"Invalid bid strategy '{bid_strategy}'. Must be one of: {', '.join(sorted(valid_strategies))}"

        patch_set: dict = {"bidStrategy": strategy}

        if bid_amount:
            patch_set["unitCost"] = {
                "amount": bid_amount,
                "currencyCode": bid_currency,
            }

        body = {"patch": {"$set": patch_set}}
        extra_headers = {"X-RestLi-Method": "PARTIAL_UPDATE"}

        data = linkedin_api_request(
            "POST",
            f"/adAccounts/{account_id}/adCampaigns/{campaign_id}",
            json_body=body,
            extra_headers=extra_headers,
        )
        if "error" in data:
            return f"Error setting bid strategy: {data['error']}"

        lines = [f"Campaign {campaign_id} bid strategy updated successfully."]
        lines.append(f"  Bid Strategy: {strategy}")
        if bid_amount:
            lines.append(f"  Bid Amount: {bid_amount} {bid_currency}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error setting bid strategy: {str(e)}"


@mcp.tool()
async def pause_resume_campaign(
    account_id: str = Field(description="LinkedIn Ad Account ID"),
    campaign_id: str = Field(description="Campaign ID to pause or resume"),
    action: str = Field(description="Action to take: 'pause' or 'resume'"),
) -> str:
    """
    Convenience tool to pause or resume a campaign.

    Args:
        account_id: The numeric ad account ID.
        campaign_id: The campaign ID.
        action: 'pause' to set PAUSED, 'resume' to set ACTIVE.

    Returns:
        Confirmation of the status change.
    """
    try:
        new_status = "PAUSED" if action.lower() == "pause" else "ACTIVE"
        body = {"patch": {"$set": {"status": new_status}}}
        extra_headers = {"X-RestLi-Method": "PARTIAL_UPDATE"}

        data = linkedin_api_request(
            "POST",
            f"/adAccounts/{account_id}/adCampaigns/{campaign_id}",
            json_body=body,
            extra_headers=extra_headers,
        )
        if "error" in data:
            return f"Error updating campaign: {data['error']}"

        return f"Campaign {campaign_id} updated successfully.\nStatus set to: {new_status}"
    except Exception as e:
        return f"Error {action}ing campaign: {str(e)}"

# ---------------------------------------------------------------------------
# D. Creative Management (3 tools)
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_creatives(
    account_id: str = Field(description="LinkedIn Ad Account ID"),
    campaign_id: str = Field(default="", description="Filter by campaign ID (optional)"),
    status_filter: str = Field(default="", description="Filter by status: ACTIVE, PAUSED, DRAFT, ARCHIVED (comma-separated)"),
    format: str = Field(default="table", description="Output format: 'table', 'json', or 'csv'"),
) -> str:
    """
    List creatives for a LinkedIn Ad Account.

    Args:
        account_id: The numeric ad account ID.
        campaign_id: Optional campaign ID to filter by.
        status_filter: Optional comma-separated statuses.
        format: Output format.

    Returns:
        A formatted list of creatives.
    """
    try:
        params: dict = {"q": "criteria"}
        raw_parts = []
        if campaign_id:
            cam_urn = format_campaign_urn(campaign_id).replace(":", "%3A")
            raw_parts.append(f"campaigns=List({cam_urn})")
        if status_filter:
            statuses = ",".join(s.strip() for s in status_filter.split(","))
            raw_parts.append(f"status=List({statuses})")
        if raw_parts:
            params["__raw_query"] = "&".join(raw_parts)

        elements = linkedin_paginated_request(f"/adAccounts/{account_id}/creatives", params=params)

        if not elements:
            return "No creatives found."

        rows = []
        for cr in elements:
            rows.append({
                "id": extract_id_from_urn(cr.get("id", "")),
                "status": cr.get("status", "N/A"),
                "campaign": extract_id_from_urn(cr.get("campaign", "")),
                "type": cr.get("type", "N/A"),
                "intendedStatus": cr.get("intendedStatus", "N/A"),
                "isServing": cr.get("isServing", "N/A"),
            })

        output_lines = [f"Creatives for Account {account_id}:"]
        output_lines.append("=" * 80)
        output_lines.append(format_output(rows, format_type=format))
        return "\n".join(output_lines)
    except Exception as e:
        return f"Error listing creatives: {str(e)}"


@mcp.tool()
async def get_creative_details(
    account_id: str = Field(description="LinkedIn Ad Account ID"),
    creative_id: str = Field(description="Creative ID to retrieve"),
) -> str:
    """
    Get full details for a specific creative.

    Args:
        account_id: The numeric ad account ID.
        creative_id: The creative ID.

    Returns:
        Detailed creative configuration.
    """
    try:
        data = linkedin_api_request("GET", f"/adAccounts/{account_id}/creatives/{creative_id}")
        if "error" in data:
            return f"Error: {data['error']}"

        lines = [f"Creative Details — {creative_id}:"]
        lines.append("=" * 60)
        for key, val in data.items():
            if isinstance(val, (dict, list)):
                val = json.dumps(val, indent=2, default=str)
            lines.append(f"  {key}: {val}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error getting creative details: {str(e)}"


@mcp.tool()
async def create_creative(
    account_id: str = Field(description="LinkedIn Ad Account ID"),
    campaign_id: str = Field(description="Campaign ID this creative belongs to"),
    content_reference: str = Field(description="URN of the content (e.g. 'urn:li:share:12345' or 'urn:li:ugcPost:12345')"),
    status: str = Field(default="ACTIVE", description="Initial status: ACTIVE, PAUSED, DRAFT"),
    call_to_action: str = Field(default="", description="CTA type: APPLY, DOWNLOAD, GET_QUOTE, LEARN_MORE, SIGN_UP, SUBSCRIBE, REGISTER, JOIN, ATTEND, REQUEST_DEMO (leave empty for none)"),
    intended_status: str = Field(default="ACTIVE", description="Intended delivery status: ACTIVE or PAUSED"),
) -> str:
    """
    Create a new creative under a campaign.

    Args:
        account_id: The numeric ad account ID.
        campaign_id: Parent campaign ID.
        content_reference: URN of the content to promote.
        status: Initial status.
        call_to_action: Optional call-to-action type.
        intended_status: Intended delivery status.

    Returns:
        Confirmation with the new creative ID.
    """
    try:
        body: dict = {
            "campaign": format_campaign_urn(campaign_id),
            "intendedStatus": intended_status.upper(),
            "content": {
                "reference": content_reference,
            },
        }

        data = linkedin_api_request("POST", f"/adAccounts/{account_id}/creatives", json_body=body)
        if "error" in data:
            return f"Error creating creative: {data['error']}"

        created_id = data.get("_created_id", "unknown")
        return (
            f"Creative created successfully.\n"
            f"ID: {created_id}\n"
            f"Campaign: {campaign_id}\n"
            f"Status: {status}\n"
            f"Content: {content_reference}"
        )
    except Exception as e:
        return f"Error creating creative: {str(e)}"

# ---------------------------------------------------------------------------
# E. Analytics & Reporting (6 tools)
# ---------------------------------------------------------------------------

_ANALYTICS_METRICS = [
    "impressions", "clicks", "costInLocalCurrency",
    "landingPageClicks", "likes", "shares", "comments",
    "follows", "conversions", "approximateUniqueImpressions",
    "videoStarts", "videoFirstQuartileCompletions",
    "videoMidpointCompletions", "videoThirdQuartileCompletions",
    "videoCompletions",
]


def _build_analytics_params(
    account_id: str,
    pivot: str,
    start_date: str,
    end_date: str,
    time_granularity: str = "DAILY",
    campaign_ids: Optional[List[str]] = None,
    campaign_group_ids: Optional[List[str]] = None,
    creative_ids: Optional[List[str]] = None,
) -> dict:
    """Build query params for the /adAnalytics endpoint.

    Restli-encoded params (dateRange, List() filters) are collected into
    __raw_query so they can be appended to the URL without URL-encoding.
    """
    params: dict = {
        "q": "analytics",
        "pivot": pivot,
        "timeGranularity": time_granularity.upper(),
    }

    # Collect Restli-encoded fragments that must not be URL-encoded
    raw_parts: list = []

    # Date range
    date_params = parse_date_params(start_date, end_date)
    raw_parts.append(date_params["__raw_query"])

    # Account scope — colons in URNs must be percent-encoded
    acct_urn = format_account_urn(account_id).replace(":", "%3A")
    raw_parts.append(f"accounts=List({acct_urn})")

    # Entity filters
    if campaign_ids:
        urns = ",".join(format_campaign_urn(c).replace(":", "%3A") for c in campaign_ids)
        raw_parts.append(f"campaigns=List({urns})")
    if campaign_group_ids:
        urns = ",".join(format_campaign_group_urn(account_id, g).replace(":", "%3A") for g in campaign_group_ids)
        raw_parts.append(f"campaignGroups=List({urns})")
    if creative_ids:
        urns = ",".join(format_creative_urn(c).replace(":", "%3A") for c in creative_ids)
        raw_parts.append(f"creatives=List({urns})")

    params["__raw_query"] = "&".join(raw_parts)

    # Explicitly request cost, engagement, and conversion fields
    raw_parts_fields = (
        "fields=costInLocalCurrency,impressions,clicks,landingPageClicks,"
        "likes,shares,comments,externalWebsiteConversions,oneClickLeads,"
        "approximateUniqueImpressions,pivotValues,dateRange"
    )
    params["__raw_query"] += "&" + raw_parts_fields

    return params


def _resolve_org_names(org_ids: list) -> dict:
    """Batch-resolve LinkedIn organization IDs -> display names via /organizations.

    Returns {id_str: name}. IDs that can't be resolved are simply omitted so the
    caller can fall back to the raw ID. Chunks requests to stay within RestLi
    limits and never raises (name resolution is best-effort).
    """
    names: dict = {}
    ids = [str(i).strip() for i in org_ids if str(i).strip().isdigit()]
    ids = list(dict.fromkeys(ids))  # dedup, preserve order
    if not ids:
        return names
    CHUNK = 50
    for i in range(0, len(ids), CHUNK):
        chunk = ids[i:i + CHUNK]
        raw = "ids=List(" + ",".join(chunk) + ")"
        try:
            data = linkedin_api_request("GET", "/organizations", params={"__raw_query": raw})
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        results = data.get("results", {})
        for oid, org in results.items():
            if not isinstance(org, dict):
                continue
            name = org.get("localizedName") or org.get("vanityName")
            if not name:
                nm = org.get("name")
                if isinstance(nm, dict):
                    loc = nm.get("localized", {})
                    if isinstance(loc, dict) and loc:
                        name = next(iter(loc.values()), None)
            if name:
                names[str(oid)] = name
    return names


def _format_analytics_results(elements: list, pivot: str, format_type: str = "table",
                              name_map: Optional[dict] = None, name_label: str = "name") -> str:
    """Format analytics results into requested format.

    If name_map is provided, a human-readable column (name_label) is inserted
    right after the pivot value, mapping the pivot ID -> name (falls back to the
    raw ID when a name isn't available).
    """
    if not elements:
        return "No analytics data found for the specified criteria."

    rows = []
    for el in elements:
        row: dict = {}
        # Pivot value — newer API versions use pivotValues (array)
        pivot_val = el.get("pivotValue", el.get("pivot", ""))
        if not pivot_val:
            pv_list = el.get("pivotValues", [])
            pivot_val = pv_list[0] if pv_list else ""
        if isinstance(pivot_val, str) and "urn:" in pivot_val:
            pivot_val = extract_id_from_urn(pivot_val)
        row["pivotValue"] = pivot_val
        if name_map is not None:
            row[name_label] = name_map.get(str(pivot_val), str(pivot_val))

        # Date range
        dr = el.get("dateRange", {})
        start = dr.get("start", {})
        end = dr.get("end", {})
        if start:
            row["date"] = f"{start.get('year', '')}-{start.get('month', '01'):02d}-{start.get('day', '01'):02d}"

        # Metrics
        impressions = int(el.get("impressions", 0))
        clicks = int(el.get("clicks", 0))
        row["impressions"] = impressions
        row["clicks"] = clicks
        row["CTR"] = f"{(clicks / impressions * 100):.2f}%" if impressions > 0 else "0.00%"
        row["costInLocalCurrency"] = el.get("costInLocalCurrency", "0")
        row["landingPageClicks"] = el.get("landingPageClicks", 0)
        row["likes"] = el.get("likes", 0)
        row["shares"] = el.get("shares", 0)
        row["comments"] = el.get("comments", 0)
        row["conversions"] = el.get("externalWebsiteConversions", el.get("conversions", 0))
        row["leads"] = el.get("oneClickLeads", 0)
        row["approximateUniqueImpressions"] = el.get("approximateUniqueImpressions", 0)
        rows.append(row)

    return format_output(rows, format_type=format_type)


@mcp.tool()
async def get_campaign_analytics(
    account_id: str = Field(description="LinkedIn Ad Account ID"),
    start_date: str = Field(description="Start date in YYYY-MM-DD format"),
    end_date: str = Field(description="End date in YYYY-MM-DD format"),
    campaign_ids: str = Field(default="", description="Comma-separated campaign IDs to filter (leave empty for all)"),
    time_granularity: str = Field(default="DAILY", description="Time granularity: DAILY, MONTHLY, or ALL"),
    format: str = Field(default="table", description="Output format: 'table', 'json', or 'csv'"),
) -> str:
    """
    Get campaign-level performance analytics.

    Metrics returned: impressions, clicks, CTR, costInLocalCurrency,
    landingPageClicks, likes, shares, comments, conversions, approximateUniqueImpressions.

    Args:
        account_id: The numeric ad account ID.
        start_date: Start date (YYYY-MM-DD).
        end_date: End date (YYYY-MM-DD).
        campaign_ids: Optional comma-separated campaign IDs.
        time_granularity: DAILY, MONTHLY, or ALL.
        format: Output format.

    Returns:
        Formatted analytics data pivoted by campaign.
    """
    try:
        cids = [c.strip() for c in campaign_ids.split(",") if c.strip()] if campaign_ids else None
        params = _build_analytics_params(
            account_id=account_id,
            pivot="CAMPAIGN",
            start_date=start_date,
            end_date=end_date,
            time_granularity=time_granularity,
            campaign_ids=cids,
        )

        elements = linkedin_paginated_request("/adAnalytics", params=params)

        output_lines = [f"Campaign Analytics for Account {account_id} ({start_date} to {end_date}):"]
        output_lines.append("=" * 100)
        output_lines.append(_format_analytics_results(elements, "CAMPAIGN", format))
        return "\n".join(output_lines)
    except Exception as e:
        return f"Error getting campaign analytics: {str(e)}"


@mcp.tool()
async def get_account_analytics(
    account_id: str = Field(description="LinkedIn Ad Account ID"),
    start_date: str = Field(description="Start date in YYYY-MM-DD format"),
    end_date: str = Field(description="End date in YYYY-MM-DD format"),
    time_granularity: str = Field(default="DAILY", description="Time granularity: DAILY, MONTHLY, or ALL"),
    format: str = Field(default="table", description="Output format: 'table', 'json', or 'csv'"),
) -> str:
    """
    Get account-level aggregate analytics.

    Metrics returned: impressions, clicks, CTR, costInLocalCurrency,
    landingPageClicks, likes, shares, comments, conversions.

    Args:
        account_id: The numeric ad account ID.
        start_date: Start date (YYYY-MM-DD).
        end_date: End date (YYYY-MM-DD).
        time_granularity: DAILY, MONTHLY, or ALL.
        format: Output format.

    Returns:
        Formatted account-level analytics.
    """
    try:
        params = _build_analytics_params(
            account_id=account_id,
            pivot="ACCOUNT",
            start_date=start_date,
            end_date=end_date,
            time_granularity=time_granularity,
        )

        elements = linkedin_paginated_request("/adAnalytics", params=params)

        output_lines = [f"Account Analytics for {account_id} ({start_date} to {end_date}):"]
        output_lines.append("=" * 100)
        output_lines.append(_format_analytics_results(elements, "ACCOUNT", format))
        return "\n".join(output_lines)
    except Exception as e:
        return f"Error getting account analytics: {str(e)}"


@mcp.tool()
async def get_creative_analytics(
    account_id: str = Field(description="LinkedIn Ad Account ID"),
    start_date: str = Field(description="Start date in YYYY-MM-DD format"),
    end_date: str = Field(description="End date in YYYY-MM-DD format"),
    creative_ids: str = Field(default="", description="Comma-separated creative IDs to filter (leave empty for all)"),
    time_granularity: str = Field(default="DAILY", description="Time granularity: DAILY, MONTHLY, or ALL"),
    format: str = Field(default="table", description="Output format: 'table', 'json', or 'csv'"),
) -> str:
    """
    Get creative-level performance analytics.

    Args:
        account_id: The numeric ad account ID.
        start_date: Start date (YYYY-MM-DD).
        end_date: End date (YYYY-MM-DD).
        creative_ids: Optional comma-separated creative IDs.
        time_granularity: DAILY, MONTHLY, or ALL.
        format: Output format.

    Returns:
        Formatted creative-level analytics.
    """
    try:
        crids = [c.strip() for c in creative_ids.split(",") if c.strip()] if creative_ids else None
        params = _build_analytics_params(
            account_id=account_id,
            pivot="CREATIVE",
            start_date=start_date,
            end_date=end_date,
            time_granularity=time_granularity,
            creative_ids=crids,
        )

        elements = linkedin_paginated_request("/adAnalytics", params=params)

        output_lines = [f"Creative Analytics for Account {account_id} ({start_date} to {end_date}):"]
        output_lines.append("=" * 100)
        output_lines.append(_format_analytics_results(elements, "CREATIVE", format))
        return "\n".join(output_lines)
    except Exception as e:
        return f"Error getting creative analytics: {str(e)}"


@mcp.tool()
async def get_campaign_group_analytics(
    account_id: str = Field(description="LinkedIn Ad Account ID"),
    start_date: str = Field(description="Start date in YYYY-MM-DD format"),
    end_date: str = Field(description="End date in YYYY-MM-DD format"),
    campaign_group_ids: str = Field(default="", description="Comma-separated campaign group IDs (leave empty for all)"),
    time_granularity: str = Field(default="DAILY", description="Time granularity: DAILY, MONTHLY, or ALL"),
    format: str = Field(default="table", description="Output format: 'table', 'json', or 'csv'"),
) -> str:
    """
    Get campaign-group-level analytics.

    Args:
        account_id: The numeric ad account ID.
        start_date: Start date (YYYY-MM-DD).
        end_date: End date (YYYY-MM-DD).
        campaign_group_ids: Optional comma-separated group IDs.
        time_granularity: DAILY, MONTHLY, or ALL.
        format: Output format.

    Returns:
        Formatted campaign group analytics.
    """
    try:
        gids = [g.strip() for g in campaign_group_ids.split(",") if g.strip()] if campaign_group_ids else None
        params = _build_analytics_params(
            account_id=account_id,
            pivot="CAMPAIGN_GROUP",
            start_date=start_date,
            end_date=end_date,
            time_granularity=time_granularity,
            campaign_group_ids=gids,
        )

        elements = linkedin_paginated_request("/adAnalytics", params=params)

        output_lines = [f"Campaign Group Analytics for Account {account_id} ({start_date} to {end_date}):"]
        output_lines.append("=" * 100)
        output_lines.append(_format_analytics_results(elements, "CAMPAIGN_GROUP", format))
        return "\n".join(output_lines)
    except Exception as e:
        return f"Error getting campaign group analytics: {str(e)}"


@mcp.tool()
async def get_demographic_analytics(
    account_id: str = Field(description="LinkedIn Ad Account ID"),
    start_date: str = Field(description="Start date in YYYY-MM-DD format"),
    end_date: str = Field(description="End date in YYYY-MM-DD format"),
    demographic_type: str = Field(description="Demographic pivot: MEMBER_JOB_TITLE, MEMBER_JOB_FUNCTION, MEMBER_SENIORITY, MEMBER_INDUSTRY, MEMBER_COMPANY_SIZE, MEMBER_COUNTRY_V2, MEMBER_REGION_V2"),
    campaign_ids: str = Field(default="", description="Comma-separated campaign IDs to filter (leave empty for all)"),
    time_granularity: str = Field(default="ALL", description="Time granularity: DAILY, MONTHLY, or ALL"),
    format: str = Field(default="table", description="Output format: 'table', 'json', or 'csv'"),
) -> str:
    """
    Get audience demographic breakdowns for campaigns.

    Supports breakdowns by job title, job function, seniority, industry,
    company size, country, and region.

    Args:
        account_id: The numeric ad account ID.
        start_date: Start date (YYYY-MM-DD).
        end_date: End date (YYYY-MM-DD).
        demographic_type: The MEMBER_* pivot type.
        campaign_ids: Optional comma-separated campaign IDs.
        time_granularity: DAILY, MONTHLY, or ALL (recommended: ALL for demographics).
        format: Output format.

    Returns:
        Formatted demographic analytics.
    """
    try:
        cids = [c.strip() for c in campaign_ids.split(",") if c.strip()] if campaign_ids else None
        pivot = demographic_type.upper()
        params = _build_analytics_params(
            account_id=account_id,
            pivot=pivot,
            start_date=start_date,
            end_date=end_date,
            time_granularity=time_granularity,
            campaign_ids=cids,
        )

        elements = linkedin_paginated_request("/adAnalytics", params=params)

        output_lines = [f"Demographic Analytics ({pivot}) for Account {account_id} ({start_date} to {end_date}):"]
        output_lines.append("=" * 100)
        output_lines.append(_format_analytics_results(elements, pivot, format))
        return "\n".join(output_lines)
    except Exception as e:
        return f"Error getting demographic analytics: {str(e)}"


@mcp.tool()
async def get_multi_pivot_analytics(
    account_id: str = Field(description="LinkedIn Ad Account ID"),
    start_date: str = Field(description="Start date in YYYY-MM-DD format"),
    end_date: str = Field(description="End date in YYYY-MM-DD format"),
    pivots: str = Field(description="Comma-separated pivots (up to 3), e.g. 'CAMPAIGN,MEMBER_COUNTRY_V2'"),
    campaign_ids: str = Field(default="", description="Comma-separated campaign IDs to filter (leave empty for all)"),
    time_granularity: str = Field(default="ALL", description="Time granularity: DAILY, MONTHLY, or ALL"),
    format: str = Field(default="table", description="Output format: 'table', 'json', or 'csv'"),
) -> str:
    """
    Get cross-dimensional analytics with multiple pivots (up to 3).

    This uses the /adAnalytics?q=statistics endpoint for multi-pivot analysis.

    Args:
        account_id: The numeric ad account ID.
        start_date: Start date (YYYY-MM-DD).
        end_date: End date (YYYY-MM-DD).
        pivots: Comma-separated pivot names (max 3).
        campaign_ids: Optional comma-separated campaign IDs.
        time_granularity: DAILY, MONTHLY, or ALL.
        format: Output format.

    Returns:
        Formatted multi-pivot analytics.

    Example:
        pivots: "CAMPAIGN,MEMBER_COUNTRY_V2"
    """
    try:
        pivot_list = [p.strip().upper() for p in pivots.split(",")]
        if len(pivot_list) > 3:
            return "Error: Maximum 3 pivots allowed."

        params: dict = {
            "q": "statistics",
            "pivots": f"List({','.join(pivot_list)})",
            "timeGranularity": time_granularity.upper(),
            "accounts": f"List({format_account_urn(account_id)})",
        }
        params.update(parse_date_params(start_date, end_date))

        if campaign_ids:
            cids = [c.strip() for c in campaign_ids.split(",") if c.strip()]
            urns = ",".join(format_campaign_urn(c) for c in cids)
            params["campaigns"] = f"List({urns})"

        elements = linkedin_paginated_request("/adAnalytics", params=params)

        if not elements:
            return "No multi-pivot analytics data found."

        # Format with pivot values
        rows = []
        for el in elements:
            row: dict = {}
            # Extract all pivot values
            pivot_values = el.get("pivotValues", [])
            for i, pv in enumerate(pivot_values):
                pivot_name = pivot_list[i] if i < len(pivot_list) else f"pivot_{i}"
                val = pv
                if isinstance(pv, str) and "urn:" in pv:
                    val = extract_id_from_urn(pv)
                row[pivot_name] = val

            # Date range
            dr = el.get("dateRange", {})
            start = dr.get("start", {})
            if start:
                row["date"] = f"{start.get('year', '')}-{start.get('month', '01'):02d}-{start.get('day', '01'):02d}"

            impressions = int(el.get("impressions", 0))
            clicks = int(el.get("clicks", 0))
            row["impressions"] = impressions
            row["clicks"] = clicks
            row["CTR"] = f"{(clicks / impressions * 100):.2f}%" if impressions > 0 else "0.00%"
            row["costInLocalCurrency"] = el.get("costInLocalCurrency", "0")
            row["conversions"] = el.get("conversions", 0)
            rows.append(row)

        output_lines = [f"Multi-Pivot Analytics for Account {account_id} ({start_date} to {end_date}):"]
        output_lines.append(f"Pivots: {', '.join(pivot_list)}")
        output_lines.append("=" * 100)
        output_lines.append(format_output(rows, format_type=format))
        return "\n".join(output_lines)
    except Exception as e:
        return f"Error getting multi-pivot analytics: {str(e)}"

# ---------------------------------------------------------------------------
# F. Targeting & Audience (3 tools)
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_targeting_facets(
    format: str = Field(default="table", description="Output format: 'table', 'json', or 'csv'"),
) -> str:
    """
    List available LinkedIn ad targeting facet types.

    Returns the targeting types (facets) that can be used when building
    campaign targeting criteria (e.g. JOB_TITLE, SENIORITY, INDUSTRY, etc.).

    Returns:
        A list of available targeting facets.
    """
    try:
        data = linkedin_api_request("GET", "/adTargetingFacets")
        if "error" in data:
            return f"Error: {data['error']}"

        elements = data.get("elements", [])
        if not elements:
            return "No targeting facets found."

        rows = []
        for facet in elements:
            rows.append({
                "name": facet.get("name", "N/A"),
                "urn": facet.get("urn", "N/A"),
                "availableEntityFinders": ", ".join(facet.get("availableEntityFinders", [])),
            })

        output_lines = ["LinkedIn Ad Targeting Facets:"]
        output_lines.append("=" * 80)
        output_lines.append(format_output(rows, format_type=format))
        return "\n".join(output_lines)
    except Exception as e:
        return f"Error getting targeting facets: {str(e)}"


@mcp.tool()
async def get_targeting_entities(
    facet_urn: str = Field(description="Targeting facet URN (e.g. 'urn:li:adTargetingFacet:titles')"),
    query: str = Field(default="", description="Search query to filter entities (e.g. 'software engineer')"),
    limit: int = Field(default=25, description="Maximum number of results to return"),
    format: str = Field(default="table", description="Output format: 'table', 'json', or 'csv'"),
) -> str:
    """
    Search for targeting entity values within a specific facet.

    Use this to find specific targeting values like job titles, industries,
    or skills to use in campaign targeting criteria.

    Args:
        facet_urn: The targeting facet URN.
        query: Search query to filter results.
        limit: Max results.
        format: Output format.

    Returns:
        Matching targeting entities.

    Example:
        facet_urn: "urn:li:adTargetingFacet:titles"
        query: "software engineer"
    """
    try:
        # queryVersion=QUERY_USES_URNS forces the response into urn/name/facetUrn
        # fields. Without it, the adTargetingFacet finder defaults to
        # QUERY_USES_MIXED and returns range facets (staffCountRanges, ageRanges,
        # seniorities, etc.) only as a legacy {"value":{"string":"urn:..."}} blob,
        # which is why those facets used to come back as all-N/A.
        if query:
            params: dict = {"q": "typeahead", "facet": facet_urn, "query": query,
                            "count": limit, "queryVersion": "QUERY_USES_URNS"}
        else:
            params = {"q": "adTargetingFacet", "facet": facet_urn,
                      "count": limit, "queryVersion": "QUERY_USES_URNS"}

        data = linkedin_api_request("GET", "/adTargetingEntities", params=params)
        if "error" in data:
            return f"Error: {data['error']}"

        elements = data.get("elements", [])
        if not elements:
            return "No targeting entities found."

        rows = []
        for entity in elements:
            urn = entity.get("urn")
            if not urn:
                # Legacy QUERY_USES_MIXED shape: {"value": {"string": "urn:..."}}
                val = entity.get("value")
                if isinstance(val, dict):
                    urn = val.get("string") or next(iter(val.values()), None)
                elif isinstance(val, str):
                    urn = val
            rows.append({
                "name": entity.get("name", "N/A"),
                "urn": urn or "N/A",
                "facetUrn": entity.get("facetUrn", facet_urn),
            })

        output_lines = [f"Targeting Entities for {facet_urn}:"]
        output_lines.append("=" * 80)
        output_lines.append(format_output(rows, format_type=format))
        return "\n".join(output_lines)
    except Exception as e:
        return f"Error getting targeting entities: {str(e)}"


@mcp.tool()
async def estimate_audience_size(
    account_id: str = Field(description="LinkedIn Ad Account ID"),
    targeting_criteria_json: str = Field(description="Targeting criteria as a JSON string. Example: {\"include\":{\"and\":[{\"or\":{\"urn:li:adTargetingFacet:titles\":[\"urn:li:title:100\"]}}]}}"),
) -> str:
    """
    Estimate the audience size for given targeting criteria.

    Provide targeting criteria in LinkedIn's targeting criteria JSON format.
    The tool returns estimated audience count.

    Args:
        account_id: The numeric ad account ID.
        targeting_criteria_json: JSON string of targeting criteria.

    Returns:
        Estimated audience size.

    Example:
        targeting_criteria_json: '{"include":{"and":[{"or":{"urn:li:adTargetingFacet:titles":["urn:li:title:100"]}}]}}'
    """
    try:
        targeting = json.loads(targeting_criteria_json)
        # /audienceCounts uses the RestLi-2.0 finder q=targetingCriteriaV2 and
        # expects targetingCriteria as a RestLi-encoded string (NOT JSON, and NO
        # account param). URN keys/values are percent-encoded so their :(), chars
        # don't collide with the RestLi structure (this matters for staffCountRange
        # values like urn:li:staffCountRange:(201,500)). Sent via __raw_query so
        # requests doesn't double-encode the structural characters.
        restli = _targeting_to_restli(targeting)
        raw = f"q=targetingCriteriaV2&targetingCriteria={restli}"
        data = linkedin_api_request("GET", "/audienceCounts", params={"__raw_query": raw})
        if "error" in data:
            return f"Error: {data['error']}"

        elements = data.get("elements", [])
        first = elements[0] if elements else {}
        total = first.get("total", "N/A")
        active = first.get("active", "N/A")

        lines = [f"Audience Size Estimate for Account {account_id}:"]
        lines.append("=" * 60)
        lines.append(f"  Total audience: {total}")
        lines.append(f"  Active audience (more likely to visit LinkedIn): {active}")
        if isinstance(total, int) and total < 300:
            lines.append("  ⚠ Below LinkedIn's 300-member minimum to run a campaign "
                         "(the API reports 0 when the true size is under 300).")
        return "\n".join(lines)
    except json.JSONDecodeError:
        return "Error: Invalid JSON in targeting_criteria_json parameter."
    except Exception as e:
        return f"Error estimating audience size: {str(e)}"

# ---------------------------------------------------------------------------
# G. Scheduling (2 tools)
# ---------------------------------------------------------------------------

@mcp.tool()
async def schedule_campaign(
    account_id: str = Field(description="LinkedIn Ad Account ID"),
    campaign_id: str = Field(description="Campaign ID to schedule"),
    start_date: str = Field(description="Start date in YYYY-MM-DD format"),
    end_date: str = Field(default="", description="End date in YYYY-MM-DD format (leave empty for no end)"),
    daily_budget_amount: str = Field(default="", description="Daily budget (leave empty to keep current)"),
    daily_budget_currency: str = Field(default="USD", description="Currency code"),
    pacing_strategy: str = Field(default="", description="LIFETIME or DAILY pacing"),
    activate: bool = Field(default=False, description="Set to true to also set status to ACTIVE"),
) -> str:
    """
    Schedule a campaign by setting start/end dates, budget, and pacing.

    This is a convenience tool that performs a partial update on the campaign
    to set scheduling-related fields. Optionally activates the campaign.

    Args:
        account_id: The numeric ad account ID.
        campaign_id: Campaign ID.
        start_date: Start date (YYYY-MM-DD).
        end_date: End date (YYYY-MM-DD, optional).
        daily_budget_amount: Optional budget update.
        daily_budget_currency: Currency code.
        pacing_strategy: Optional pacing strategy.
        activate: Whether to set status to ACTIVE.

    Returns:
        Confirmation of the schedule update.
    """
    try:
        patch_set: dict = {}

        run_schedule: dict = {"start": iso_to_epoch_ms(start_date)}
        if end_date:
            run_schedule["end"] = iso_to_epoch_ms(end_date)
        patch_set["runSchedule"] = run_schedule

        if daily_budget_amount:
            patch_set["dailyBudget"] = {
                "amount": daily_budget_amount,
                "currencyCode": daily_budget_currency,
            }
        if pacing_strategy:
            patch_set["pacingStrategy"] = pacing_strategy.upper()
        if activate:
            patch_set["status"] = "ACTIVE"

        body = {"patch": {"$set": patch_set}}
        extra_headers = {"X-RestLi-Method": "PARTIAL_UPDATE"}

        data = linkedin_api_request(
            "POST",
            f"/adAccounts/{account_id}/adCampaigns/{campaign_id}",
            json_body=body,
            extra_headers=extra_headers,
        )
        if "error" in data:
            return f"Error scheduling campaign: {data['error']}"

        lines = [f"Campaign {campaign_id} scheduled successfully:"]
        lines.append(f"  Start: {start_date}")
        if end_date:
            lines.append(f"  End: {end_date}")
        if daily_budget_amount:
            lines.append(f"  Daily Budget: {daily_budget_amount} {daily_budget_currency}")
        if pacing_strategy:
            lines.append(f"  Pacing: {pacing_strategy}")
        if activate:
            lines.append(f"  Status: ACTIVE")
        return "\n".join(lines)
    except Exception as e:
        return f"Error scheduling campaign: {str(e)}"


@mcp.tool()
async def schedule_campaign_group(
    account_id: str = Field(description="LinkedIn Ad Account ID"),
    group_id: str = Field(description="Campaign group ID to schedule"),
    start_date: str = Field(description="Start date in YYYY-MM-DD format"),
    end_date: str = Field(default="", description="End date in YYYY-MM-DD format (leave empty for no end)"),
    total_budget_amount: str = Field(default="", description="Total budget (leave empty to keep current)"),
    total_budget_currency: str = Field(default="USD", description="Currency code"),
    activate: bool = Field(default=False, description="Set to true to also set status to ACTIVE"),
) -> str:
    """
    Schedule a campaign group by setting start/end dates and total budget.

    This is a convenience tool that performs a partial update on the campaign group
    to set scheduling-related fields. Optionally activates the group.

    Args:
        account_id: The numeric ad account ID.
        group_id: Campaign group ID.
        start_date: Start date (YYYY-MM-DD).
        end_date: End date (YYYY-MM-DD, optional).
        total_budget_amount: Optional total budget update.
        total_budget_currency: Currency code.
        activate: Whether to set status to ACTIVE.

    Returns:
        Confirmation of the schedule update.
    """
    try:
        patch_set: dict = {}

        run_schedule: dict = {"start": iso_to_epoch_ms(start_date)}
        if end_date:
            run_schedule["end"] = iso_to_epoch_ms(end_date)
        patch_set["runSchedule"] = run_schedule

        if total_budget_amount:
            patch_set["totalBudget"] = {
                "amount": total_budget_amount,
                "currencyCode": total_budget_currency,
            }
        if activate:
            patch_set["status"] = "ACTIVE"

        body = {"patch": {"$set": patch_set}}
        extra_headers = {"X-RestLi-Method": "PARTIAL_UPDATE"}

        data = linkedin_api_request(
            "POST",
            f"/adAccounts/{account_id}/adCampaignGroups/{group_id}",
            json_body=body,
            extra_headers=extra_headers,
        )
        if "error" in data:
            return f"Error scheduling campaign group: {data['error']}"

        lines = [f"Campaign group {group_id} scheduled successfully:"]
        lines.append(f"  Start: {start_date}")
        if end_date:
            lines.append(f"  End: {end_date}")
        if total_budget_amount:
            lines.append(f"  Total Budget: {total_budget_amount} {total_budget_currency}")
        if activate:
            lines.append(f"  Status: ACTIVE")
        return "\n".join(lines)
    except Exception as e:
        return f"Error scheduling campaign group: {str(e)}"

# ---------------------------------------------------------------------------
# H. Company / Audience / Conversion / Lead Gen (8 tools)
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_company_performance(
    account_id: str = Field(description="LinkedIn Ad Account ID"),
    start_date: str = Field(description="Start date in YYYY-MM-DD format"),
    end_date: str = Field(description="End date in YYYY-MM-DD format"),
    campaign_ids: str = Field(default="", description="Comma-separated campaign IDs to filter (leave empty for all)"),
    limit: int = Field(default=200, description="Maximum number of company rows to return"),
    resolve_names: bool = Field(default=True, description="Resolve company IDs to readable names via organizationsLookup (adds a few lookups; set false for speed)"),
    format: str = Field(default="table", description="Output format: 'table', 'json', or 'csv'"),
) -> str:
    """
    Get performance broken down by company (organization) that saw or clicked ads.

    Shows which companies your ads reached, with impressions, clicks, and cost.
    By default the LinkedIn company IDs are resolved to readable company names
    (a `company` column); set resolve_names=False to skip the extra lookups.

    Args:
        account_id: The numeric ad account ID.
        start_date: Start date (YYYY-MM-DD).
        end_date: End date (YYYY-MM-DD).
        campaign_ids: Optional comma-separated campaign IDs.
        limit: Max rows to return.
        resolve_names: Resolve org IDs to names (default True).
        format: Output format.

    Returns:
        Formatted company-level performance data (company name + metrics).
    """
    try:
        cids = [c.strip() for c in campaign_ids.split(",") if c.strip()] if campaign_ids else None
        params = _build_analytics_params(
            account_id=account_id,
            pivot="MEMBER_COMPANY",
            start_date=start_date,
            end_date=end_date,
            time_granularity="ALL",
            campaign_ids=cids,
        )

        elements = linkedin_paginated_request("/adAnalytics", params=params, max_results=limit)

        name_map = None
        if resolve_names and elements:
            org_ids = []
            for el in elements:
                pv = el.get("pivotValue") or (el.get("pivotValues") or [""])[0]
                if isinstance(pv, str) and pv:
                    org_ids.append(extract_id_from_urn(pv))
            name_map = _resolve_org_names(org_ids)

        output_lines = [f"Company Performance for Account {account_id} ({start_date} to {end_date}):"]
        output_lines.append("=" * 100)
        output_lines.append(_format_analytics_results(elements, "MEMBER_COMPANY", format,
                                                      name_map=name_map, name_label="company"))
        return "\n".join(output_lines)
    except Exception as e:
        return f"Error getting company performance: {str(e)}"


@mcp.tool()
async def compare_performance(
    account_id: str = Field(description="LinkedIn Ad Account ID"),
    date_range_1_start: str = Field(description="Period 1 start date (YYYY-MM-DD)"),
    date_range_1_end: str = Field(description="Period 1 end date (YYYY-MM-DD)"),
    date_range_2_start: str = Field(description="Period 2 start date (YYYY-MM-DD)"),
    date_range_2_end: str = Field(description="Period 2 end date (YYYY-MM-DD)"),
    format: str = Field(default="table", description="Output format: 'table', 'json', or 'csv'"),
) -> str:
    """
    Compare account performance across two date ranges side by side.

    Useful for period-over-period analysis (e.g. this month vs. last month).

    Args:
        account_id: The numeric ad account ID.
        date_range_1_start: First period start.
        date_range_1_end: First period end.
        date_range_2_start: Second period start.
        date_range_2_end: Second period end.
        format: Output format.

    Returns:
        Side-by-side comparison with absolute and percentage change.
    """
    try:
        # Fetch period 1
        params1 = _build_analytics_params(
            account_id=account_id, pivot="ACCOUNT",
            start_date=date_range_1_start, end_date=date_range_1_end,
            time_granularity="ALL",
        )
        elements1 = linkedin_paginated_request("/adAnalytics", params=params1)

        # Fetch period 2
        params2 = _build_analytics_params(
            account_id=account_id, pivot="ACCOUNT",
            start_date=date_range_2_start, end_date=date_range_2_end,
            time_granularity="ALL",
        )
        elements2 = linkedin_paginated_request("/adAnalytics", params=params2)

        def _sum_metric(elements, key):
            return sum(float(el.get(key, 0)) for el in elements)

        metrics = ["impressions", "clicks", "costInLocalCurrency",
                    "landingPageClicks", "likes", "shares", "comments",
                    "conversions", "approximateUniqueImpressions"]

        rows = []
        for m in metrics:
            v1 = _sum_metric(elements1, m)
            v2 = _sum_metric(elements2, m)
            change = v2 - v1
            pct = f"{(change / v1 * 100):.1f}%" if v1 != 0 else "N/A"
            rows.append({
                "metric": m,
                f"period1 ({date_range_1_start} to {date_range_1_end})": f"{v1:,.2f}",
                f"period2 ({date_range_2_start} to {date_range_2_end})": f"{v2:,.2f}",
                "change": f"{change:+,.2f}",
                "change%": pct,
            })

        # Add calculated CTR
        imp1 = _sum_metric(elements1, "impressions")
        clk1 = _sum_metric(elements1, "clicks")
        imp2 = _sum_metric(elements2, "impressions")
        clk2 = _sum_metric(elements2, "clicks")
        ctr1 = (clk1 / imp1 * 100) if imp1 > 0 else 0
        ctr2 = (clk2 / imp2 * 100) if imp2 > 0 else 0
        ctr_change = ctr2 - ctr1
        rows.append({
            "metric": "CTR",
            f"period1 ({date_range_1_start} to {date_range_1_end})": f"{ctr1:.2f}%",
            f"period2 ({date_range_2_start} to {date_range_2_end})": f"{ctr2:.2f}%",
            "change": f"{ctr_change:+.2f}pp",
            "change%": f"{(ctr_change / ctr1 * 100):.1f}%" if ctr1 > 0 else "N/A",
        })

        output_lines = [f"Performance Comparison for Account {account_id}:"]
        output_lines.append("=" * 100)
        output_lines.append(format_output(rows, format_type=format))
        return "\n".join(output_lines)
    except Exception as e:
        return f"Error comparing performance: {str(e)}"


@mcp.tool()
async def get_audience_reach(
    account_id: str = Field(description="LinkedIn Ad Account ID"),
    start_date: str = Field(description="Start date in YYYY-MM-DD format"),
    end_date: str = Field(description="End date in YYYY-MM-DD format"),
    format: str = Field(default="table", description="Output format: 'table', 'json', or 'csv'"),
) -> str:
    """
    Get audience reach and frequency metrics for an account over a period.

    Shows approximate unique impressions (reach) alongside total impressions
    to help understand frequency and audience saturation.

    Args:
        account_id: The numeric ad account ID.
        start_date: Start date (YYYY-MM-DD).
        end_date: End date (YYYY-MM-DD).
        format: Output format.

    Returns:
        Reach metrics including unique impressions, total impressions, and frequency.
    """
    try:
        params = _build_analytics_params(
            account_id=account_id, pivot="ACCOUNT",
            start_date=start_date, end_date=end_date,
            time_granularity="ALL",
        )
        elements = linkedin_paginated_request("/adAnalytics", params=params)

        if not elements:
            return "No reach data found for the specified criteria."

        total_impressions = sum(int(el.get("impressions", 0)) for el in elements)
        unique_impressions = sum(int(el.get("approximateUniqueImpressions", 0)) for el in elements)
        frequency = (total_impressions / unique_impressions) if unique_impressions > 0 else 0

        rows = [{
            "totalImpressions": f"{total_impressions:,}",
            "uniqueReach": f"{unique_impressions:,}",
            "avgFrequency": f"{frequency:.2f}",
            "clicks": f"{sum(int(el.get('clicks', 0)) for el in elements):,}",
            "costInLocalCurrency": f"{sum(float(el.get('costInLocalCurrency', 0)) for el in elements):,.2f}",
        }]

        output_lines = [f"Audience Reach for Account {account_id} ({start_date} to {end_date}):"]
        output_lines.append("=" * 80)
        output_lines.append(format_output(rows, format_type=format))
        return "\n".join(output_lines)
    except Exception as e:
        return f"Error getting audience reach: {str(e)}"


@mcp.tool()
async def list_saved_audiences(
    account_id: str = Field(description="LinkedIn Ad Account ID"),
    format: str = Field(default="table", description="Output format: 'table', 'json', or 'csv'"),
) -> str:
    """
    List saved/matched audiences on a LinkedIn Ad Account.

    Returns DMP segments and matched audiences available for targeting.

    Args:
        account_id: The numeric ad account ID.
        format: Output format.

    Returns:
        List of saved audiences with ID, name, and status.
    """
    try:
        params = {
            "q": "account",
            "account": format_account_urn(account_id),
        }
        elements = linkedin_paginated_request(f"/adSegments", params=params)

        if not elements:
            return "No saved audiences found for this account."

        rows = []
        for seg in elements:
            rows.append({
                "id": extract_id_from_urn(seg.get("id", "")),
                "name": seg.get("name", "N/A"),
                "status": seg.get("status", "N/A"),
                "type": seg.get("type", "N/A"),
                "matchedCount": seg.get("matchedCount", "N/A"),
            })

        output_lines = [f"Saved Audiences for Account {account_id}:"]
        output_lines.append("=" * 80)
        output_lines.append(format_output(rows, format_type=format))
        return "\n".join(output_lines)
    except Exception as e:
        return f"Error listing saved audiences: {str(e)}"


@mcp.tool()
async def get_conversion_performance(
    account_id: str = Field(description="LinkedIn Ad Account ID"),
    start_date: str = Field(description="Start date in YYYY-MM-DD format"),
    end_date: str = Field(description="End date in YYYY-MM-DD format"),
    format: str = Field(default="table", description="Output format: 'table', 'json', or 'csv'"),
) -> str:
    """
    Get conversion-specific performance metrics by campaign.

    Returns conversion counts, values, cost-per-conversion, and conversion rate
    for each campaign in the account.

    Args:
        account_id: The numeric ad account ID.
        start_date: Start date (YYYY-MM-DD).
        end_date: End date (YYYY-MM-DD).
        format: Output format.

    Returns:
        Conversion-focused metrics per campaign.
    """
    try:
        params = _build_analytics_params(
            account_id=account_id, pivot="CAMPAIGN",
            start_date=start_date, end_date=end_date,
            time_granularity="ALL",
        )
        elements = linkedin_paginated_request("/adAnalytics", params=params)

        if not elements:
            return "No conversion data found for the specified criteria."

        rows = []
        for el in elements:
            pivot_val = el.get("pivotValue", "")
            if not pivot_val:
                pv_list = el.get("pivotValues", [])
                pivot_val = pv_list[0] if pv_list else ""
            if isinstance(pivot_val, str) and "urn:" in pivot_val:
                pivot_val = extract_id_from_urn(pivot_val)

            impressions = int(el.get("impressions", 0))
            clicks = int(el.get("clicks", 0))
            conversions = float(el.get("conversions", 0))
            cost = float(el.get("costInLocalCurrency", 0))
            conv_rate = f"{(conversions / clicks * 100):.2f}%" if clicks > 0 else "0.00%"
            cost_per_conv = f"{(cost / conversions):.2f}" if conversions > 0 else "N/A"

            rows.append({
                "campaignId": pivot_val,
                "impressions": impressions,
                "clicks": clicks,
                "conversions": conversions,
                "conversionRate": conv_rate,
                "costPerConversion": cost_per_conv,
                "totalCost": f"{cost:.2f}",
                "externalWebsiteConversions": el.get("externalWebsiteConversions", 0),
                "externalWebsitePostClickConversions": el.get("externalWebsitePostClickConversions", 0),
                "externalWebsitePostViewConversions": el.get("externalWebsitePostViewConversions", 0),
            })

        output_lines = [f"Conversion Performance for Account {account_id} ({start_date} to {end_date}):"]
        output_lines.append("=" * 100)
        output_lines.append(format_output(rows, format_type=format))
        return "\n".join(output_lines)
    except Exception as e:
        return f"Error getting conversion performance: {str(e)}"


@mcp.tool()
async def get_lead_gen_performance(
    account_id: str = Field(description="LinkedIn Ad Account ID"),
    start_date: str = Field(description="Start date in YYYY-MM-DD format"),
    end_date: str = Field(description="End date in YYYY-MM-DD format"),
    format: str = Field(default="table", description="Output format: 'table', 'json', or 'csv'"),
) -> str:
    """
    Get Lead Gen Form submission metrics by campaign.

    Returns lead form opens, completions, and completion rate alongside
    standard performance metrics.

    Args:
        account_id: The numeric ad account ID.
        start_date: Start date (YYYY-MM-DD).
        end_date: End date (YYYY-MM-DD).
        format: Output format.

    Returns:
        Lead gen performance metrics per campaign.
    """
    try:
        params = _build_analytics_params(
            account_id=account_id, pivot="CAMPAIGN",
            start_date=start_date, end_date=end_date,
            time_granularity="ALL",
        )
        elements = linkedin_paginated_request("/adAnalytics", params=params)

        if not elements:
            return "No lead gen data found for the specified criteria."

        rows = []
        for el in elements:
            pivot_val = el.get("pivotValue", "")
            if not pivot_val:
                pv_list = el.get("pivotValues", [])
                pivot_val = pv_list[0] if pv_list else ""
            if isinstance(pivot_val, str) and "urn:" in pivot_val:
                pivot_val = extract_id_from_urn(pivot_val)

            opens = int(el.get("leadGenerationMailContactInfoShares", el.get("oneClickLeadFormOpens", 0)))
            completions = int(el.get("oneClickLeads", el.get("leadGenerationMailInterestedClicks", 0)))
            comp_rate = f"{(completions / opens * 100):.2f}%" if opens > 0 else "N/A"
            cost = float(el.get("costInLocalCurrency", 0))
            cost_per_lead = f"{(cost / completions):.2f}" if completions > 0 else "N/A"

            rows.append({
                "campaignId": pivot_val,
                "impressions": int(el.get("impressions", 0)),
                "clicks": int(el.get("clicks", 0)),
                "leadFormOpens": opens,
                "leadFormCompletions": completions,
                "completionRate": comp_rate,
                "costPerLead": cost_per_lead,
                "totalCost": f"{cost:.2f}",
            })

        output_lines = [f"Lead Gen Performance for Account {account_id} ({start_date} to {end_date}):"]
        output_lines.append("=" * 100)
        output_lines.append(format_output(rows, format_type=format))
        return "\n".join(output_lines)
    except Exception as e:
        return f"Error getting lead gen performance: {str(e)}"


@mcp.tool()
async def list_conversions(
    account_id: str = Field(description="LinkedIn Ad Account ID"),
    format: str = Field(default="table", description="Output format: 'table', 'json', or 'csv'"),
) -> str:
    """
    List conversion tracking rules configured on a LinkedIn Ad Account.

    Returns conversion definitions including name, type, and attribution settings.

    Args:
        account_id: The numeric ad account ID.
        format: Output format.

    Returns:
        List of conversion tracking rules.
    """
    try:
        params = {
            "q": "account",
            "account": format_account_urn(account_id),
        }
        elements = linkedin_paginated_request("/conversions", params=params)

        if not elements:
            return "No conversion tracking rules found for this account."

        rows = []
        for conv in elements:
            rows.append({
                "id": extract_id_from_urn(conv.get("id", "")),
                "name": conv.get("name", "N/A"),
                "type": conv.get("type", "N/A"),
                "enabled": conv.get("enabled", "N/A"),
                "postClickAttributionWindowSize": conv.get("postClickAttributionWindowSize", "N/A"),
                "viewThroughAttributionWindowSize": conv.get("viewThroughAttributionWindowSize", "N/A"),
                "attributionType": conv.get("attributionType", "N/A"),
            })

        output_lines = [f"Conversion Rules for Account {account_id}:"]
        output_lines.append("=" * 80)
        output_lines.append(format_output(rows, format_type=format))
        return "\n".join(output_lines)
    except Exception as e:
        return f"Error listing conversions: {str(e)}"


@mcp.tool()
async def list_lead_forms(
    account_id: str = Field(description="LinkedIn Ad Account ID"),
    format: str = Field(default="table", description="Output format: 'table', 'json', or 'csv'"),
) -> str:
    """
    List Lead Gen Forms on a LinkedIn Ad Account.

    Returns form definitions including name, status, and creation date.

    Args:
        account_id: The numeric ad account ID.
        format: Output format.

    Returns:
        List of Lead Gen Forms.
    """
    try:
        params = {
            "q": "owner",
            "owner": format_account_urn(account_id),
        }
        elements = linkedin_paginated_request("/leadForms", params=params)

        if not elements:
            return "No Lead Gen Forms found for this account."

        rows = []
        for form in elements:
            rows.append({
                "id": extract_id_from_urn(form.get("id", "")),
                "name": form.get("name", "N/A"),
                "status": form.get("status", "N/A"),
                "headline": form.get("headline", "N/A"),
                "description": form.get("description", "N/A"),
                "createdAt": epoch_ms_to_iso(form.get("createdAt", 0)),
            })

        output_lines = [f"Lead Gen Forms for Account {account_id}:"]
        output_lines.append("=" * 80)
        output_lines.append(format_output(rows, format_type=format))
        return "\n".join(output_lines)
    except Exception as e:
        return f"Error listing lead forms: {str(e)}"

# ---------------------------------------------------------------------------
# I. Batch Resolvers (5 tools)
# ---------------------------------------------------------------------------

@mcp.tool()
async def resolve_campaigns(
    campaign_ids: str = Field(description="Comma-separated campaign IDs to resolve"),
    account_id: str = Field(default="", description="LinkedIn Ad Account ID (optional, improves lookup)"),
    format: str = Field(default="table", description="Output format: 'table', 'json', or 'csv'"),
) -> str:
    """
    Resolve multiple campaign IDs to human-readable names in one call.

    Args:
        campaign_ids: Comma-separated campaign IDs.
        account_id: Optional account ID for scoped lookup.
        format: Output format.

    Returns:
        Mapping of campaign IDs to names and statuses.
    """
    try:
        ids = [c.strip() for c in campaign_ids.split(",") if c.strip()]
        if not ids:
            return "No campaign IDs provided."

        rows = []
        for cid in ids:
            acct = account_id if account_id else "*"
            if account_id:
                data = linkedin_api_request("GET", f"/adAccounts/{account_id}/adCampaigns/{cid}")
            else:
                data = linkedin_api_request("GET", f"/adCampaigns/{cid}")

            if "error" in data:
                rows.append({"id": cid, "name": "NOT FOUND", "status": "N/A"})
            else:
                rows.append({
                    "id": cid,
                    "name": data.get("name", "N/A"),
                    "status": data.get("status", "N/A"),
                    "type": data.get("type", "N/A"),
                    "objectiveType": data.get("objectiveType", "N/A"),
                })

        output_lines = ["Resolved Campaigns:"]
        output_lines.append("=" * 80)
        output_lines.append(format_output(rows, format_type=format))
        return "\n".join(output_lines)
    except Exception as e:
        return f"Error resolving campaigns: {str(e)}"


@mcp.tool()
async def resolve_creatives(
    creative_ids: str = Field(description="Comma-separated creative IDs to resolve"),
    account_id: str = Field(default="", description="LinkedIn Ad Account ID (optional, improves lookup)"),
    format: str = Field(default="table", description="Output format: 'table', 'json', or 'csv'"),
) -> str:
    """
    Resolve multiple creative IDs to their details in one call.

    Args:
        creative_ids: Comma-separated creative IDs.
        account_id: Optional account ID.
        format: Output format.

    Returns:
        Mapping of creative IDs to status and campaign association.
    """
    try:
        ids = [c.strip() for c in creative_ids.split(",") if c.strip()]
        if not ids:
            return "No creative IDs provided."

        rows = []
        for crid in ids:
            if account_id:
                data = linkedin_api_request("GET", f"/adAccounts/{account_id}/creatives/{crid}")
            else:
                data = linkedin_api_request("GET", f"/creatives/{crid}")

            if "error" in data:
                rows.append({"id": crid, "status": "NOT FOUND", "campaign": "N/A"})
            else:
                rows.append({
                    "id": crid,
                    "status": data.get("status", "N/A"),
                    "intendedStatus": data.get("intendedStatus", "N/A"),
                    "campaign": extract_id_from_urn(data.get("campaign", "")),
                    "type": data.get("type", "N/A"),
                })

        output_lines = ["Resolved Creatives:"]
        output_lines.append("=" * 80)
        output_lines.append(format_output(rows, format_type=format))
        return "\n".join(output_lines)
    except Exception as e:
        return f"Error resolving creatives: {str(e)}"


@mcp.tool()
async def resolve_campaign_groups(
    group_ids: str = Field(description="Comma-separated campaign group IDs to resolve"),
    account_id: str = Field(default="", description="LinkedIn Ad Account ID (optional, improves lookup)"),
    format: str = Field(default="table", description="Output format: 'table', 'json', or 'csv'"),
) -> str:
    """
    Resolve multiple campaign group IDs to human-readable names in one call.

    Args:
        group_ids: Comma-separated group IDs.
        account_id: Optional account ID.
        format: Output format.

    Returns:
        Mapping of group IDs to names and statuses.
    """
    try:
        ids = [g.strip() for g in group_ids.split(",") if g.strip()]
        if not ids:
            return "No group IDs provided."

        rows = []
        for gid in ids:
            if account_id:
                data = linkedin_api_request("GET", f"/adAccounts/{account_id}/adCampaignGroups/{gid}")
            else:
                data = linkedin_api_request("GET", f"/adCampaignGroups/{gid}")

            if "error" in data:
                rows.append({"id": gid, "name": "NOT FOUND", "status": "N/A"})
            else:
                rows.append({
                    "id": gid,
                    "name": data.get("name", "N/A"),
                    "status": data.get("status", "N/A"),
                })

        output_lines = ["Resolved Campaign Groups:"]
        output_lines.append("=" * 80)
        output_lines.append(format_output(rows, format_type=format))
        return "\n".join(output_lines)
    except Exception as e:
        return f"Error resolving campaign groups: {str(e)}"


@mcp.tool()
async def resolve_audiences(
    audience_ids: str = Field(description="Comma-separated audience/segment IDs to resolve"),
    account_id: str = Field(default="", description="LinkedIn Ad Account ID (optional)"),
    format: str = Field(default="table", description="Output format: 'table', 'json', or 'csv'"),
) -> str:
    """
    Resolve multiple audience/segment IDs to names in one call.

    Args:
        audience_ids: Comma-separated audience IDs.
        account_id: Optional account ID.
        format: Output format.

    Returns:
        Mapping of audience IDs to names and details.
    """
    try:
        ids = [a.strip() for a in audience_ids.split(",") if a.strip()]
        if not ids:
            return "No audience IDs provided."

        rows = []
        for aid in ids:
            data = linkedin_api_request("GET", f"/adSegments/{aid}")

            if "error" in data:
                rows.append({"id": aid, "name": "NOT FOUND", "status": "N/A"})
            else:
                rows.append({
                    "id": aid,
                    "name": data.get("name", "N/A"),
                    "status": data.get("status", "N/A"),
                    "type": data.get("type", "N/A"),
                    "matchedCount": data.get("matchedCount", "N/A"),
                })

        output_lines = ["Resolved Audiences:"]
        output_lines.append("=" * 80)
        output_lines.append(format_output(rows, format_type=format))
        return "\n".join(output_lines)
    except Exception as e:
        return f"Error resolving audiences: {str(e)}"


@mcp.tool()
async def resolve_accounts(
    account_ids: str = Field(description="Comma-separated ad account IDs to resolve"),
    format: str = Field(default="table", description="Output format: 'table', 'json', or 'csv'"),
) -> str:
    """
    Resolve multiple ad account IDs to names and details in one call.

    Args:
        account_ids: Comma-separated account IDs.
        format: Output format.

    Returns:
        Mapping of account IDs to names, statuses, and currencies.
    """
    try:
        ids = [a.strip() for a in account_ids.split(",") if a.strip()]
        if not ids:
            return "No account IDs provided."

        rows = []
        for aid in ids:
            data = linkedin_api_request("GET", f"/adAccounts/{aid}")

            if "error" in data:
                rows.append({"id": aid, "name": "NOT FOUND", "status": "N/A"})
            else:
                rows.append({
                    "id": aid,
                    "name": data.get("name", "N/A"),
                    "status": data.get("status", "N/A"),
                    "type": data.get("type", "N/A"),
                    "currency": data.get("currency", "N/A"),
                })

        output_lines = ["Resolved Accounts:"]
        output_lines.append("=" * 80)
        output_lines.append(format_output(rows, format_type=format))
        return "\n".join(output_lines)
    except Exception as e:
        return f"Error resolving accounts: {str(e)}"

# ---------------------------------------------------------------------------
# J. Weekday-Only Scheduling (4 tools)
# ---------------------------------------------------------------------------

SCHEDULES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schedules.json")


def _load_schedules() -> dict:
    """Load the schedules.json file."""
    if not os.path.exists(SCHEDULES_FILE):
        return {"weekday_only": []}
    try:
        with open(SCHEDULES_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {"weekday_only": []}


def _save_schedules(data: dict) -> None:
    """Save data to schedules.json."""
    with open(SCHEDULES_FILE, "w") as f:
        json.dump(data, f, indent=2)


@mcp.tool()
async def add_weekday_schedule(
    account_id: str = Field(description="LinkedIn Ad Account ID"),
    campaign_id: str = Field(description="Campaign ID to schedule for weekday-only delivery"),
    campaign_name: str = Field(default="", description="Campaign name (for reference only)"),
    timezone: str = Field(default="America/New_York", description="Timezone for schedule evaluation (e.g. 'America/New_York', 'UTC')"),
    resume_time: str = Field(default="06:00", description="Time to resume campaign on Monday (HH:MM, 24h format). Ignored if hours is provided."),
    pause_time: str = Field(default="18:00", description="Time to pause campaign on Friday (HH:MM, 24h format). Ignored if hours is provided."),
    hours: str = Field(default="", description="Optional JSON string with per-day active hours, e.g. '{\"mon\":[6,7,8,...],\"tue\":[...],...}'. Keys: sun,mon,tue,wed,thu,fri,sat. Values: arrays of integers 0-23. If provided, resume_time/pause_time are ignored."),
) -> str:
    """
    Add a campaign to weekday-only scheduling.

    Since LinkedIn's API does not support native dayparting, this tool stores
    a scheduling rule in a local JSON file. Use `run_weekday_scheduler` or
    the standalone scheduler.py script (via cron) to automatically pause/resume
    campaigns based on the schedule.

    You can either provide simple resume_time/pause_time (weekdays only), or
    a full per-day hours grid for fine-grained control over every day and hour.

    Args:
        account_id: The numeric ad account ID.
        campaign_id: The campaign ID.
        campaign_name: Optional human-readable campaign name.
        timezone: Timezone for evaluating the schedule.
        resume_time: Time to resume on Monday (HH:MM). Ignored if hours is provided.
        pause_time: Time to pause on Friday (HH:MM). Ignored if hours is provided.
        hours: Optional JSON string with per-day active hours grid.

    Returns:
        Confirmation that the schedule rule was added.
    """
    try:
        schedules = _load_schedules()

        # Check for duplicate
        for rule in schedules["weekday_only"]:
            if rule["campaign_id"] == str(campaign_id) and rule["account_id"] == str(account_id):
                return f"Campaign {campaign_id} is already scheduled for weekday-only delivery."

        rule = {
            "account_id": str(account_id),
            "campaign_id": str(campaign_id),
            "campaign_name": campaign_name,
            "timezone": timezone,
            "added_at": datetime.now().isoformat(timespec="seconds"),
        }

        if hours:
            parsed_hours = json.loads(hours)
            rule["hours"] = parsed_hours
        else:
            # Build hours grid from resume_time/pause_time for backward compat
            rt = int(resume_time.split(":")[0])
            pt = int(pause_time.split(":")[0])
            wd_hours = list(range(rt, pt))
            rule["hours"] = {
                "sun": [], "mon": wd_hours[:], "tue": wd_hours[:],
                "wed": wd_hours[:], "thu": wd_hours[:], "fri": wd_hours[:],
                "sat": [],
            }
            # Also store legacy fields for backward compat
            rule["resume_time"] = resume_time
            rule["pause_time"] = pause_time

        schedules["weekday_only"].append(rule)
        _save_schedules(schedules)

        lines = [f"Campaign {campaign_id} added to weekday-only schedule."]
        lines.append(f"  Account: {account_id}")
        lines.append(f"  Timezone: {timezone}")
        if "hours" in rule and not hours:
            lines.append(f"  Resume: Monday at {resume_time}")
            lines.append(f"  Pause: Friday at {pause_time}")
        else:
            active_days = [d for d in ["sun","mon","tue","wed","thu","fri","sat"] if rule["hours"].get(d)]
            lines.append(f"  Active days: {', '.join(active_days) if active_days else 'none'}")
        lines.append(f"\nTo activate, run `run_weekday_scheduler` or set up scheduler.py via cron:")
        lines.append(f"  # Example cron (every hour):")
        lines.append(f"  0 * * * * cd {os.path.dirname(os.path.abspath(__file__))} && python scheduler.py")
        return "\n".join(lines)
    except Exception as e:
        return f"Error adding weekday schedule: {str(e)}"


@mcp.tool()
async def remove_weekday_schedule(
    account_id: str = Field(description="LinkedIn Ad Account ID"),
    campaign_id: str = Field(description="Campaign ID to remove from weekday-only scheduling"),
) -> str:
    """
    Remove a campaign from weekday-only scheduling.

    Args:
        account_id: The numeric ad account ID.
        campaign_id: The campaign ID to remove.

    Returns:
        Confirmation that the schedule rule was removed.
    """
    try:
        schedules = _load_schedules()
        original_count = len(schedules["weekday_only"])
        schedules["weekday_only"] = [
            r for r in schedules["weekday_only"]
            if not (r["campaign_id"] == str(campaign_id) and r["account_id"] == str(account_id))
        ]

        if len(schedules["weekday_only"]) == original_count:
            return f"Campaign {campaign_id} was not found in weekday-only schedules."

        _save_schedules(schedules)
        return f"Campaign {campaign_id} removed from weekday-only scheduling."
    except Exception as e:
        return f"Error removing weekday schedule: {str(e)}"


@mcp.tool()
async def list_weekday_schedules(
    format: str = Field(default="table", description="Output format: 'table', 'json', or 'csv'"),
) -> str:
    """
    List all campaigns with weekday-only scheduling rules.

    Returns:
        A formatted list of all weekday-only schedule rules.
    """
    try:
        schedules = _load_schedules()
        rules = schedules.get("weekday_only", [])

        if not rules:
            return "No weekday-only schedules configured."

        rows = []
        for r in rules:
            row = {
                "account_id": r.get("account_id", ""),
                "campaign_id": r.get("campaign_id", ""),
                "campaign_name": r.get("campaign_name", ""),
                "timezone": r.get("timezone", ""),
                "added_at": r.get("added_at", ""),
            }
            if "hours" in r:
                # Summarize hours grid
                active_days = [d for d in ["mon","tue","wed","thu","fri","sat","sun"] if r["hours"].get(d)]
                total_hours = sum(len(r["hours"].get(d, [])) for d in ["sun","mon","tue","wed","thu","fri","sat"])
                row["schedule"] = f"{len(active_days)} days, {total_hours} hrs/wk"
            else:
                row["schedule"] = f"Mon {r.get('resume_time','')} – Fri {r.get('pause_time','')}"
            rows.append(row)

        output_lines = ["Weekday-Only Schedules:"]
        output_lines.append("=" * 100)
        output_lines.append(format_output(rows, format_type=format))
        return "\n".join(output_lines)
    except Exception as e:
        return f"Error listing weekday schedules: {str(e)}"


@mcp.tool()
async def run_weekday_scheduler() -> str:
    """
    Manually trigger the weekday-only scheduler.

    Reads scheduling rules from schedules.json, checks the current day and time
    in each rule's timezone, and pauses or resumes campaigns accordingly:
    - On weekdays (Mon-Fri): resumes campaigns that should be active
    - On weekends (Sat-Sun): pauses campaigns that should be paused
    - On Friday after pause_time: pauses campaigns
    - On Monday before resume_time: keeps campaigns paused

    Returns:
        A summary of actions taken (campaigns paused, resumed, or unchanged).
    """
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        try:
            from backports.zoneinfo import ZoneInfo
        except ImportError:
            return "Error: zoneinfo module not available. Install Python 3.9+ or 'backports.zoneinfo'."

    DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

    try:
        schedules = _load_schedules()
        rules = schedules.get("weekday_only", [])

        if not rules:
            return "No weekday-only schedules configured. Nothing to do."

        actions = []
        for rule in rules:
            account_id = rule["account_id"]
            campaign_id = rule["campaign_id"]
            tz_name = rule.get("timezone", "UTC")

            try:
                tz = ZoneInfo(tz_name)
            except Exception:
                actions.append(f"  SKIP {campaign_id}: invalid timezone '{tz_name}'")
                continue

            now = datetime.now(tz)
            weekday = now.weekday()  # 0=Mon, 6=Sun
            current_time = now.strftime("%H:%M")

            if "hours" in rule:
                # New hours-grid model
                day_key = DAY_KEYS[weekday]
                active_hours = rule["hours"].get(day_key, [])
                desired_status = "ACTIVE" if now.hour in active_hours else "PAUSED"
            else:
                # Legacy resume_time/pause_time model
                resume_time_str = rule.get("resume_time", "06:00")
                pause_time_str = rule.get("pause_time", "18:00")
                if weekday >= 5:
                    desired_status = "PAUSED"
                elif weekday == 4 and current_time >= pause_time_str:
                    desired_status = "PAUSED"
                elif weekday == 0 and current_time < resume_time_str:
                    desired_status = "PAUSED"
                else:
                    desired_status = "ACTIVE"

            # Apply the status
            body = {"patch": {"$set": {"status": desired_status}}}
            extra_headers = {"X-RestLi-Method": "PARTIAL_UPDATE"}
            data = linkedin_api_request(
                "POST",
                f"/adAccounts/{account_id}/adCampaigns/{campaign_id}",
                json_body=body,
                extra_headers=extra_headers,
            )

            campaign_name = rule.get("campaign_name", campaign_id)
            if "error" in data:
                actions.append(f"  ERROR {campaign_name} ({campaign_id}): {data['error']}")
            else:
                actions.append(f"  {desired_status} -> {campaign_name} ({campaign_id}) [tz={tz_name}, day={now.strftime('%A')}, time={current_time}]")

        lines = ["Weekday Scheduler Results:"]
        lines.append("=" * 80)
        lines.extend(actions)
        lines.append(f"\nProcessed {len(rules)} rule(s).")
        return "\n".join(lines)
    except Exception as e:
        return f"Error running weekday scheduler: {str(e)}"


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------

@mcp.resource("linkedinads://reference")
def linkedin_ads_reference() -> str:
    """LinkedIn Ads entity hierarchy, campaign types/objectives, status lifecycle, analytics pivots, and URN formats."""
    return """
# LinkedIn Ads Reference

## Entity Hierarchy
Account -> Campaign Group -> Campaign -> Creative

## URN Formats
- Account:        urn:li:sponsoredAccount:{id}
- Campaign Group: urn:li:sponsoredCampaignGroup:{id}
- Campaign:       urn:li:sponsoredCampaign:{id}
- Creative:       urn:li:sponsoredCreative:{id}

## Campaign Objectives
- BRAND_AWARENESS — Maximize impressions
- WEBSITE_VISITS — Drive traffic to your website
- ENGAGEMENT — Increase social actions (likes, comments, shares)
- VIDEO_VIEWS — Maximize video views
- LEAD_GENERATION — Collect leads via LinkedIn Lead Gen Forms
- WEBSITE_CONVERSIONS — Drive conversions on your website
- JOB_APPLICANTS — Drive job applications
- TALENT_LEADS — Generate talent leads

## Campaign Types
- SPONSORED_UPDATES — Sponsored Content (feed ads)
- SPONSORED_INMAILS — Message Ads (InMail)
- TEXT_ADS — Text Ads (sidebar)
- DYNAMIC — Dynamic Ads

## Status Lifecycle
- DRAFT -> ACTIVE -> PAUSED -> ACTIVE (resume)
- DRAFT -> ACTIVE -> ARCHIVED
- DRAFT -> ACTIVE -> CANCELED
- Any state -> ARCHIVED (terminal)
- Any state -> CANCELED (terminal)

## Cost Types
- CPC — Cost per click
- CPM — Cost per thousand impressions
- CPV — Cost per view (video)

## Analytics Pivots (single-pivot)
- ACCOUNT
- CAMPAIGN_GROUP
- CAMPAIGN
- CREATIVE
- MEMBER_JOB_TITLE
- MEMBER_JOB_FUNCTION
- MEMBER_SENIORITY
- MEMBER_INDUSTRY
- MEMBER_COMPANY_SIZE
- MEMBER_COUNTRY_V2
- MEMBER_REGION_V2

## Time Granularities
- DAILY
- MONTHLY
- ALL (aggregate)

## API Headers Required
- Authorization: Bearer {access_token}
- Linkedin-Version: 202605
- X-Restli-Protocol-Version: 2.0.0

## Partial Updates
Use POST with header X-RestLi-Method: PARTIAL_UPDATE
Body: {"patch": {"$set": {field: value, ...}}}

## Scheduling
Set runSchedule.start and runSchedule.end (epoch milliseconds) on campaigns and campaign groups.
"""


@mcp.resource("linkedinads://targeting-guide")
def linkedin_ads_targeting_guide() -> str:
    """How to build targeting criteria JSON with examples."""
    return """
# LinkedIn Ads Targeting Guide

## Overview
Targeting criteria use an AND/OR structure:
- Top level: AND (all criteria groups must match)
- Within each group: OR (any value in the group can match)

## JSON Structure
```json
{
  "include": {
    "and": [
      {
        "or": {
          "urn:li:adTargetingFacet:titles": [
            "urn:li:title:100",
            "urn:li:title:200"
          ]
        }
      },
      {
        "or": {
          "urn:li:adTargetingFacet:locations": [
            "urn:li:geo:103644278"
          ]
        }
      }
    ]
  },
  "exclude": {
    "or": {
      "urn:li:adTargetingFacet:industries": [
        "urn:li:industry:4"
      ]
    }
  }
}
```

## Common Targeting Facets
| Facet URN | Description | Example Entity |
|-----------|-------------|----------------|
| urn:li:adTargetingFacet:locations | Geographic locations | urn:li:geo:103644278 (US) |
| urn:li:adTargetingFacet:titles | Job titles | urn:li:title:100 |
| urn:li:adTargetingFacet:seniorities | Seniority levels | urn:li:seniority:8 (Director) |
| urn:li:adTargetingFacet:industries | Industries | urn:li:industry:4 |
| urn:li:adTargetingFacet:skills | Member skills | urn:li:skill:123 |
| urn:li:adTargetingFacet:staffCountRanges | Company size | urn:li:staffCountRange:5 (51-200) |
| urn:li:adTargetingFacet:degrees | Education level | urn:li:degree:100 |
| urn:li:adTargetingFacet:jobFunctions | Job functions | urn:li:function:12 |

## Workflow
1. Use `get_targeting_facets` to see all available facet types
2. Use `get_targeting_entities` to search for specific values within a facet
3. Build your targeting criteria JSON using the URNs returned
4. Use `estimate_audience_size` to validate your audience
5. Apply targeting to a campaign via `create_campaign` or `update_campaign`

## Tips
- Always include at least a location targeting facet
- Minimum audience size is typically 300 members
- Use audience estimation before launching to ensure reach
- Combine facets to narrow your audience (AND logic)
- Use multiple values within a facet to broaden (OR logic)
"""

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

@mcp.prompt("linkedin_ads_workflow")
def linkedin_ads_workflow() -> str:
    """Recommended step-by-step workflow for using the LinkedIn Ads MCP tools."""
    return """
I'll help you manage your LinkedIn Ads. Here's the recommended workflow:

1. **Discover accounts:**
   - `list_accounts()` — Find your ad account IDs

2. **Review account setup:**
   - `get_account_details(account_id="YOUR_ID")` — Check currency, status, budget

3. **Explore campaigns:**
   - `list_campaign_groups(account_id="YOUR_ID")` — See campaign groups
   - `list_campaigns(account_id="YOUR_ID")` — See campaigns
   - `list_creatives(account_id="YOUR_ID")` — See ad creatives

4. **Analyze performance:**
   - `get_account_analytics(account_id="YOUR_ID", start_date="2025-05-01", end_date="2025-05-31")` — Account overview
   - `get_campaign_analytics(...)` — Campaign-level metrics
   - `get_creative_analytics(...)` — Creative-level metrics
   - `get_demographic_analytics(...)` — Audience breakdowns

5. **Create new campaigns:**
   - `create_campaign_group(...)` — Create a campaign group first
   - `create_campaign(...)` — Create a campaign in the group
   - `create_creative(...)` — Add creatives to the campaign

6. **Schedule and activate:**
   - `schedule_campaign(...)` — Set dates, budget, pacing
   - `pause_resume_campaign(...)` — Control delivery

7. **Targeting:**
   - `get_targeting_facets()` — See available targeting types
   - `get_targeting_entities(...)` — Search targeting values
   - `estimate_audience_size(...)` — Validate your audience
"""


@mcp.prompt("linkedin_ads_analytics_help")
def linkedin_ads_analytics_help() -> str:
    """Guide to LinkedIn Ads analytics metrics, pivots, and date ranges."""
    return """
# LinkedIn Ads Analytics Guide

## Available Metrics
- **impressions** — Number of times your ad was shown
- **clicks** — Total clicks on your ad
- **CTR** — Click-through rate (calculated: clicks/impressions)
- **costInLocalCurrency** — Total spend in account currency
- **landingPageClicks** — Clicks that led to your landing page
- **likes** — Social likes on your ad
- **shares** — Social shares of your ad
- **comments** — Comments on your ad
- **conversions** — Conversion actions tracked
- **approximateUniqueImpressions** — Estimated unique reach

## Analytics Pivots
Use different pivots to slice your data:
- **ACCOUNT** — Account-level aggregate
- **CAMPAIGN_GROUP** — By campaign group
- **CAMPAIGN** — By campaign
- **CREATIVE** — By creative/ad

## Demographic Pivots
- **MEMBER_JOB_TITLE** — By job title
- **MEMBER_JOB_FUNCTION** — By job function
- **MEMBER_SENIORITY** — By seniority level
- **MEMBER_INDUSTRY** — By industry
- **MEMBER_COMPANY_SIZE** — By company size
- **MEMBER_COUNTRY_V2** — By country
- **MEMBER_REGION_V2** — By region

## Multi-Pivot Analysis
Use `get_multi_pivot_analytics` for cross-dimensional analysis (up to 3 pivots).
Example: CAMPAIGN + MEMBER_COUNTRY_V2 to see campaign performance by country.

## Date Ranges
All date parameters use YYYY-MM-DD format:
- start_date: "2025-05-01"
- end_date: "2025-05-31"

## Time Granularity
- **DAILY** — One row per day
- **MONTHLY** — One row per month
- **ALL** — Single aggregate row (recommended for demographics)

## Output Formats
All analytics tools support: table, json, csv
"""


@mcp.prompt("linkedin_ads_campaign_creation_help")
def linkedin_ads_campaign_creation_help() -> str:
    """Step-by-step guide to creating a LinkedIn Ads campaign."""
    return """
# LinkedIn Ads Campaign Creation Guide

## Step 1: Create a Campaign Group
Campaign groups organize campaigns and control overall budget/schedule.

```
create_campaign_group(
    account_id="511389977",
    name="Q3 2025 Product Launch",
    status="DRAFT",
    total_budget_amount="5000.00",
    total_budget_currency="USD",
    start_date="2025-07-01",
    end_date="2025-09-30"
)
```

## Step 2: Build Targeting
Research your audience before creating the campaign.

```
# Find job titles
get_targeting_entities(facet_urn="urn:li:adTargetingFacet:titles", query="software engineer")

# Check audience size
estimate_audience_size(
    account_id="511389977",
    targeting_criteria_json='{"include":{"and":[{"or":{"urn:li:adTargetingFacet:locations":["urn:li:geo:103644278"]}}]}}'
)
```

## Step 3: Create Campaign
```
create_campaign(
    account_id="511389977",
    name="Product Launch - Website Visits",
    campaign_group_id="GROUP_ID",
    objective_type="WEBSITE_VISITS",
    campaign_type="SPONSORED_UPDATES",
    daily_budget_amount="100.00",
    daily_budget_currency="USD",
    cost_type="CPC",
    bid_amount="5.00",
    status="DRAFT",
    start_date="2025-07-01",
    end_date="2025-09-30"
)
```

## Step 4: Add Creative
Creatives reference existing content (shares or UGC posts).
```
create_creative(
    account_id="511389977",
    campaign_id="CAMPAIGN_ID",
    content_reference="urn:li:share:12345",
    call_to_action="LEARN_MORE"
)
```

## Step 5: Schedule and Activate
```
schedule_campaign(
    account_id="511389977",
    campaign_id="CAMPAIGN_ID",
    start_date="2025-07-01",
    end_date="2025-09-30",
    daily_budget_amount="100.00",
    pacing_strategy="DAILY",
    activate=True
)
```

## Common Objectives
| Objective | Best For | Typical Cost Type |
|-----------|----------|-------------------|
| BRAND_AWARENESS | Reach | CPM |
| WEBSITE_VISITS | Traffic | CPC |
| ENGAGEMENT | Social actions | CPM |
| VIDEO_VIEWS | Video campaigns | CPV |
| LEAD_GENERATION | Lead forms | CPM |
| WEBSITE_CONVERSIONS | Conversions | CPC |

## Tips
- Start with DRAFT status and review before activating
- Set a reasonable daily budget (LinkedIn minimum is ~$10/day)
- Use audience estimation to validate targeting
- Monitor performance after launch with analytics tools
"""


# ---------------------------------------------------------------------------
# Creative pipeline tools (image upload -> dark post -> single-image ad)
# Backed by creative_pipeline.py — full single-image ad creation + bulk.
# ---------------------------------------------------------------------------
import creative_pipeline as _cp


@mcp.tool()
async def upload_image(
    image_path: str = Field(description="Local path to the image file to upload"),
    owner_org_urn: str = Field(default="", description="Organization (Page) URN, e.g. urn:li:organization:123. Defaults to LINKEDIN_ORG_URN."),
) -> str:
    """Upload an image to the LinkedIn media library and return its image URN."""
    try:
        urn = _cp.upload_image(image_path, owner_org_urn or None)
        return f"Image uploaded.\nImage URN: {urn}"
    except Exception as e:
        return f"Error uploading image: {e}"


@mcp.tool()
async def create_single_image_ad(
    account_id: str = Field(description="LinkedIn Ad Account ID"),
    campaign_id: str = Field(description="Campaign (ad set) ID the ad belongs to"),
    image_path: str = Field(description="Local path to the image file"),
    intro_text: str = Field(description="Introductory/post text (max ~600 chars)"),
    headline: str = Field(description="Headline shown under the image (max ~200 chars)"),
    destination_url: str = Field(description="Landing page URL (https://...)"),
    call_to_action: str = Field(default="LEARN_MORE", description="CTA: LEARN_MORE, REQUEST_DEMO, SIGN_UP, DOWNLOAD, REGISTER, ..."),
    owner_org_urn: str = Field(default="", description="Organization (Page) URN. Defaults to LINKEDIN_ORG_URN."),
    status: str = Field(default="DRAFT", description="DRAFT (safe, no spend), ACTIVE (goes to review), or PAUSED (only after approved)"),
) -> str:
    """Create a complete single-image ad: uploads the image, creates the sponsored post, and creates the ad."""
    try:
        out = _cp.create_single_image_ad(
            account_id, campaign_id, image_path, intro_text, headline,
            destination_url, call_to_action, owner_org_urn or None, status,
        )
        return (
            "Single-image ad created.\n"
            f"Creative: {out['creative']}\n"
            f"Post: {out['post_urn']}\n"
            f"Image: {out['image_urn']}"
        )
    except Exception as e:
        return f"Error creating single-image ad: {e}"


@mcp.tool()
async def bulk_create_single_image_ads(
    account_id: str = Field(description="LinkedIn Ad Account ID"),
    campaign_id: str = Field(description="Campaign (ad set) ID for all ads"),
    csv_path: str = Field(description="CSV columns: image_path, intro_text, headline, call_to_action, destination_url"),
    owner_org_urn: str = Field(default="", description="Organization (Page) URN. Defaults to LINKEDIN_ORG_URN."),
    status: str = Field(default="DRAFT", description="DRAFT (safe, no spend), ACTIVE (goes to review), or PAUSED (only after approved)"),
) -> str:
    """Create many single-image ads from a CSV (one ad per row)."""
    try:
        results = _cp.bulk_create_from_csv(account_id, campaign_id, csv_path, owner_org_urn or None, status)
        ok = sum(1 for r in results if r.get("ok"))
        lines = [("OK  " if r.get("ok") else "ERR ") + f"row {r['row']}: " + (r.get("creative", "") if r.get("ok") else r.get("error", "")) for r in results]
        return f"Bulk create: {ok}/{len(results)} ads created.\n" + "\n".join(lines)
    except Exception as e:
        return f"Error in bulk create: {e}"



@mcp.tool()
async def list_pages(
    format: str = Field(default="table", description="Output format: table, json, or csv"),
) -> str:
    """List the LinkedIn Pages (organizations) you administer, with org URNs.

    Use the returned organization URN as owner_org_urn when creating ads.
    Requires the r_organization_admin / rw_organization_admin scope.
    """
    try:
        data = linkedin_api_request(
            "GET", "/organizationAcls",
            params={
                "q": "roleAssignee", "role": "ADMINISTRATOR", "state": "APPROVED", "count": 100,
                "__raw_query": "projection=(paging,elements*(role,state,organization~(id,localizedName)))",
            },
        )
        if isinstance(data, dict) and "error" in data:
            return f"Error listing pages: {data['error']}"
        rows = []
        for el in data.get("elements", []):
            org = el.get("organization", "")
            dec = el.get("organization~", {}) or {}
            rows.append({
                "name": dec.get("localizedName", ""),
                "organization_urn": org,
                "id": org.split(":")[-1] if org else "",
                "role": el.get("role", ""),
            })
        if not rows:
            return "No administered Pages found (check the r_organization_admin scope)."
        return format_output(rows, format)
    except Exception as e:
        return f"Error listing pages: {e}"



@mcp.tool()
async def resolve_page_for_account(
    account_id: str = Field(description="LinkedIn Ad Account ID"),
) -> str:
    """Show which Page (organization URN) will own ads created for this ad account.

    Uses account_pages.json / LINKEDIN_ACCOUNT_PAGES, then LINKEDIN_ORG_URN.
    """
    try:
        return f"Account {account_id} -> {_cp.resolve_page_for_account(account_id)}"
    except Exception as e:
        return f"{e}"



@mcp.tool()
async def set_campaign_targeting(
    account_id: str = Field(description="LinkedIn Ad Account ID"),
    campaign_id: str = Field(description="Campaign (ad set) ID to update"),
    targeting_criteria_json: str = Field(description="Targeting criteria JSON (same shape as estimate_audience_size), e.g. {\"include\":{\"and\":[{\"or\":{\"urn:li:adTargetingFacet:industries\":[\"urn:li:industry:48\"]}}]}}"),
) -> str:
    """Write/replace the targeting on a campaign (PATCH targetingCriteria).

    Build the criteria with get_targeting_facets / get_targeting_entities, validate
    size with estimate_audience_size, then set it here.
    """
    try:
        criteria = json.loads(targeting_criteria_json)
        body = {"patch": {"$set": {"targetingCriteria": criteria}}}
        data = linkedin_api_request(
            "POST", f"/adAccounts/{account_id}/adCampaigns/{campaign_id}",
            json_body=body, extra_headers={"X-RestLi-Method": "PARTIAL_UPDATE"},
        )
        if isinstance(data, dict) and "error" in data:
            return f"Error setting targeting: {data['error']}"
        return f"Targeting updated on campaign {campaign_id}."
    except json.JSONDecodeError as e:
        return f"Invalid targeting JSON: {e}"
    except Exception as e:
        return f"Error setting targeting: {e}"


@mcp.tool()
async def duplicate_ad(
    account_id: str = Field(description="LinkedIn Ad Account ID"),
    source_creative_id: str = Field(description="Creative (ad) ID to clone"),
    target_campaign_id: str = Field(description="Campaign (ad set) ID to create the copy in"),
    status: str = Field(default="DRAFT", description="DRAFT, ACTIVE, or PAUSED"),
) -> str:
    """Duplicate an existing ad (its image + copy) into another campaign / ad set."""
    try:
        enc = format_creative_urn(source_creative_id).replace(":", "%3A")
        src = linkedin_api_request("GET", f"/adAccounts/{account_id}/creatives/{enc}")
        if isinstance(src, dict) and "error" in src:
            return f"Error reading source creative: {src['error']}"
        content = src.get("content", {})
        if not content.get("reference"):
            return f"Source creative has no content reference; cannot duplicate. Raw: {src}"
        body = {
            "campaign": format_campaign_urn(target_campaign_id),
            "intendedStatus": status.upper(),
            "content": content,
        }
        data = linkedin_api_request("POST", f"/adAccounts/{account_id}/creatives", json_body=body)
        if isinstance(data, dict) and "error" in data:
            return f"Error creating duplicate: {data['error']}"
        return f"Ad duplicated -> {data.get('_created_id', 'unknown')} in campaign {target_campaign_id} (status {status})."
    except Exception as e:
        return f"Error duplicating ad: {e}"



@mcp.tool()
async def duplicate_campaign(
    account_id: str = Field(description="LinkedIn Ad Account ID"),
    source_campaign_id: str = Field(description="Campaign (ad set) ID to clone"),
    new_name: str = Field(default="", description="Name for the copy (defaults to '<source> (copy)')"),
    status: str = Field(default="DRAFT", description="ACTIVE, PAUSED, or DRAFT"),
) -> str:
    """Duplicate a campaign — settings, budget, bid and targeting — into the same campaign group."""
    try:
        src = linkedin_api_request("GET", f"/adAccounts/{account_id}/adCampaigns/{source_campaign_id}")
        if isinstance(src, dict) and "error" in src:
            return f"Error reading source campaign: {src['error']}"
        copy_fields = [
            "campaignGroup", "type", "costType", "objectiveType", "locale",
            "dailyBudget", "totalBudget", "unitCost", "bidStrategy", "pacingStrategy",
            "runSchedule", "targetingCriteria", "format",
            "audienceExpansionEnabled", "offsiteDeliveryEnabled",
        ]
        body = {"account": format_account_urn(account_id), "status": status.upper()}
        for fld in copy_fields:
            if src.get(fld) not in (None, ""):
                body[fld] = src[fld]
        body["name"] = new_name or (src.get("name", "Campaign") + " (copy)")
        data = linkedin_api_request("POST", f"/adAccounts/{account_id}/adCampaigns", json_body=body)
        if isinstance(data, dict) and "error" in data:
            return f"Error creating duplicate campaign: {data['error']}"
        return f"Campaign duplicated -> {data.get('_created_id', 'unknown')} (status {status}). Name: {body['name']}"
    except Exception as e:
        return f"Error duplicating campaign: {e}"


@mcp.tool()
async def create_saved_audience(
    account_id: str = Field(description="LinkedIn Ad Account ID"),
    name: str = Field(description="Name for the saved audience / targeting template"),
    targeting_criteria_json: str = Field(description="Targeting criteria JSON (same shape as estimate_audience_size)"),
) -> str:
    """Save a reusable targeting template (saved audience) on the account."""
    try:
        criteria = json.loads(targeting_criteria_json)
        body = {"account": format_account_urn(account_id), "name": name, "targetingCriteria": criteria}
        data = linkedin_api_request("POST", f"/adAccounts/{account_id}/adTargetingTemplates", json_body=body)
        if isinstance(data, dict) and "error" in data:
            return f"Error saving audience: {data['error']}"
        return f"Saved audience created -> {data.get('_created_id', 'unknown')} ('{name}')."
    except json.JSONDecodeError as e:
        return f"Invalid targeting JSON: {e}"
    except Exception as e:
        return f"Error saving audience: {e}"


@mcp.tool()
async def set_campaign_budget(
    account_id: str = Field(description="LinkedIn Ad Account ID"),
    campaign_id: str = Field(description="Campaign (ad set) ID to update"),
    daily_budget: str = Field(default="", description="New daily budget amount (optional)"),
    total_budget: str = Field(default="", description="New total/lifetime budget amount (optional)"),
    currency: str = Field(default="USD", description="Currency code"),
) -> str:
    """Update a campaign's daily and/or total (lifetime) budget."""
    try:
        s: dict = {}
        if daily_budget:
            s["dailyBudget"] = {"amount": daily_budget, "currencyCode": currency}
        if total_budget:
            s["totalBudget"] = {"amount": total_budget, "currencyCode": currency}
        if not s:
            return "Provide daily_budget and/or total_budget."
        data = linkedin_api_request(
            "POST", f"/adAccounts/{account_id}/adCampaigns/{campaign_id}",
            json_body={"patch": {"$set": s}}, extra_headers={"X-RestLi-Method": "PARTIAL_UPDATE"},
        )
        if isinstance(data, dict) and "error" in data:
            return f"Error setting budget: {data['error']}"
        return f"Budget updated on campaign {campaign_id}."
    except Exception as e:
        return f"Error setting budget: {e}"



if __name__ == "__main__":
    mcp.run(transport="stdio")
