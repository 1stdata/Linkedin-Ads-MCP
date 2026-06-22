"""
creative_pipeline.py
--------------------
Self-contained LinkedIn single-image ad creation pipeline.

This is the missing piece of the ads server: it uploads an image to the
LinkedIn media library, creates the underlying sponsored (dark) post, and
creates the ad (sponsoredCreative) that points at it — the full chain that
the Marketing API requires and that the UI does for you.

Designed to be reused three ways:
  1. Imported by the MCP server (linkedin_ads_server.py) as new tools.
  2. Imported by the CLI (bulk_create_ads.py).
  3. Imported by the Railway app / scheduler.

Auth: reuses the same token resolution as the server —
  - LINKEDIN_TOKEN_PATH (json file with {"access_token": ...}), else
  - LINKEDIN_ACCESS_TOKEN env var.
Requires the rw_ads scope (sponsored content). The image owner / post author
must be the LinkedIn *organization* URN (the company Page), e.g.
  urn:li:organization:1234567   (set LINKEDIN_ORG_URN to avoid passing it each call)

NOTE: This is v1, written to LinkedIn's documented Images + Posts + Creatives
spec (REST, versioned). Run it once against the account and share any API
error text — the post payload for headline/destination is the most likely
field to need a small tweak per API version.
"""

from __future__ import annotations
import csv
import json
import os
import time
from typing import Optional

import requests

API_BASE = "https://api.linkedin.com/rest"
API_VERSION = os.environ.get("LINKEDIN_API_VERSION", "202605")
DEFAULT_ORG_URN = os.environ.get("LINKEDIN_ORG_URN", "")

VALID_CTAS = {
    "APPLY", "DOWNLOAD", "VIEW_QUOTE", "LEARN_MORE", "SIGN_UP", "SUBSCRIBE",
    "REGISTER", "JOIN", "ATTEND", "REQUEST_DEMO", "SEE_MORE", "GET_QUOTE",
}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def get_token() -> str:
    """Resolve a LinkedIn access token from token file or env (matches the server)."""
    path = os.environ.get("LINKEDIN_TOKEN_PATH")
    if path and os.path.exists(path):
        try:
            with open(path) as f:
                data = json.load(f)
            tok = data.get("access_token")
            exp = data.get("expires_at", 0)
            if tok and (exp == 0 or time.time() < exp - 60):
                return tok
        except (json.JSONDecodeError, IOError):
            pass
    tok = os.environ.get("LINKEDIN_ACCESS_TOKEN")
    if tok:
        return tok
    raise RuntimeError(
        "No LinkedIn access token. Set LINKEDIN_ACCESS_TOKEN or LINKEDIN_TOKEN_PATH "
        "(and ensure the token has the rw_ads scope)."
    )


def _headers(token: Optional[str] = None) -> dict:
    return {
        "Authorization": f"Bearer {token or get_token()}",
        "Linkedin-Version": API_VERSION,
        "X-Restli-Protocol-Version": "2.0.0",
        "Content-Type": "application/json",
    }


def _created_urn(resp: requests.Response) -> str:
    return resp.headers.get("x-restli-id", resp.headers.get("X-RestLi-Id", ""))


try:
    from PIL import Image as _PILImage
except Exception:
    _PILImage = None


def _prepare_image(path: str, max_dim: int = 1500, target_kb: int = 450) -> str:
    """Downscale/compress large images so uploads are fast. Returns a file path.

    LinkedIn displays ads small, so a ~1500px JPEG is plenty and uploads in
    seconds even on a slow connection. Falls back to the original if Pillow is
    missing or anything goes wrong.
    """
    try:
        if _PILImage is None or os.path.getsize(path) <= target_kb * 1024:
            return path
        import tempfile
        im = _PILImage.open(path).convert("RGB")
        im.thumbnail((max_dim, max_dim))
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        im.save(tmp.name, "JPEG", quality=85, optimize=True)
        return tmp.name
    except Exception:
        return path


def _get(path: str, params: Optional[dict] = None, token: Optional[str] = None) -> dict:
    resp = requests.get(f"{API_BASE}{path}", headers=_headers(token), params=params or {}, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"GET {path} failed ({resp.status_code}): {resp.text}")
    try:
        return resp.json()
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Multi-account: map each ad account to the Page that owns its ads
# ---------------------------------------------------------------------------
def _load_account_page_map() -> dict:
    """{account_id: org_urn} from LINKEDIN_ACCOUNT_PAGES env (JSON) or account_pages.json."""
    raw = os.environ.get("LINKEDIN_ACCOUNT_PAGES")
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "account_pages.json")
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def list_admin_pages(token: Optional[str] = None) -> list:
    """Return [{organization_urn, id, name, role}] for every Page you administer."""
    data = _get(
        "/organizationAcls",
        {"q": "roleAssignee", "role": "ADMINISTRATOR", "state": "APPROVED", "count": 100},
        token,
    )
    out = []
    for el in data.get("elements", []):
        org = el.get("organization", "")
        oid = org.split(":")[-1] if org else ""
        name = ""
        if oid:
            try:
                name = _get(f"/organizations/{oid}", token=token).get("localizedName", "")
            except Exception:
                pass
        out.append({"organization_urn": org, "id": oid, "name": name, "role": el.get("role", "")})
    return out


def resolve_page_for_account(account_id: str, token: Optional[str] = None) -> str:
    """Pick the org (Page) URN to own ads for a given ad account.

    Order: explicit map (LINKEDIN_ACCOUNT_PAGES env or account_pages.json) ->
    LINKEDIN_ORG_URN default -> error with guidance.
    """
    amap = _load_account_page_map()
    if str(account_id) in amap:
        return amap[str(account_id)]
    if DEFAULT_ORG_URN:
        return DEFAULT_ORG_URN
    raise ValueError(
        f"No Page mapped for account {account_id}. Add it to account_pages.json "
        f'(e.g. {{"{account_id}": "urn:li:organization:XXXX"}}) or set LINKEDIN_ORG_URN. '
        f"Call list_admin_pages() to see your Pages."
    )


# ---------------------------------------------------------------------------
# Step 1 — upload an image to the media library
# ---------------------------------------------------------------------------
def upload_image(image_path: str, owner_urn: Optional[str] = None,
                 token: Optional[str] = None) -> str:
    """Upload a local image file and return its image URN (urn:li:image:...)."""
    owner = owner_urn or DEFAULT_ORG_URN
    if not owner:
        raise ValueError("owner_urn (organization URN) is required. Set LINKEDIN_ORG_URN.")
    token = token or get_token()

    # 1a. Initialize the upload
    init = requests.post(
        f"{API_BASE}/images?action=initializeUpload",
        headers=_headers(token),
        json={"initializeUploadRequest": {"owner": owner}}, timeout=30,
    )
    if init.status_code >= 400:
        raise RuntimeError(f"initializeUpload failed ({init.status_code}): {init.text}")
    value = init.json().get("value", {})
    upload_url = value.get("uploadUrl")
    image_urn = value.get("image")
    if not upload_url or not image_urn:
        raise RuntimeError(f"initializeUpload returned no uploadUrl/image: {init.text}")

    # 1b. PUT the binary (compress first so big PNGs upload fast)
    upload_path = _prepare_image(image_path)
    with open(upload_path, "rb") as f:
        data = f.read()
    put = requests.put(upload_url, headers={"Authorization": f"Bearer {token}"}, data=data, timeout=180)
    if put.status_code not in (200, 201):
        raise RuntimeError(f"image upload PUT failed ({put.status_code}): {put.text}")
    return image_urn


# ---------------------------------------------------------------------------
# Step 2 — create the sponsored (dark) post that backs the ad
# ---------------------------------------------------------------------------
def create_link_post(owner_urn: str, image_urn: str, intro_text: str,
                     headline: str, destination_url: str, account_id: str,
                     description: str = "", token: Optional[str] = None) -> str:
    """Create a Direct Sponsored Content link post (image + headline → URL).

    Returns the post URN (urn:li:share:... or urn:li:ugcPost:...).
    adContext + feedDistribution=NONE registers it as DSC (a dark ad post),
    tied to the ad account so it never shows on the Page feed.
    """
    body = {
        "adContext": {
            "dscAdAccount": f"urn:li:sponsoredAccount:{account_id}",
            "dscStatus": "ACTIVE",
        },
        "author": owner_urn,
        "commentary": intro_text,
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "NONE",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "content": {
            "article": {
                "source": destination_url,
                "title": headline,
                "thumbnail": image_urn,
                "description": description,
            }
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": True,
    }
    resp = requests.post(f"{API_BASE}/posts", headers=_headers(token), json=body, timeout=60)
    if resp.status_code >= 400:
        raise RuntimeError(f"create post failed ({resp.status_code}): {resp.text}")
    post_urn = _created_urn(resp)
    if not post_urn:
        raise RuntimeError(f"create post returned no x-restli-id. Body: {resp.text}")
    return post_urn


# ---------------------------------------------------------------------------
# Step 3 — create the ad (sponsoredCreative) referencing the post
# ---------------------------------------------------------------------------
def create_creative(account_id: str, campaign_id: str, post_urn: str,
                    call_to_action: str = "LEARN_MORE", status: str = "DRAFT",
                    token: Optional[str] = None) -> str:
    """Create the ad under a campaign (ad set). Returns the creative URN/id."""
    body = {
        "campaign": f"urn:li:sponsoredCampaign:{campaign_id}",
        "intendedStatus": status.upper(),
        "content": {"reference": post_urn},
    }
    resp = requests.post(
        f"{API_BASE}/adAccounts/{account_id}/creatives",
        headers=_headers(token), json=body, timeout=60,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"create creative failed ({resp.status_code}): {resp.text}")
    return _created_urn(resp) or resp.json().get("id", "unknown")


# ---------------------------------------------------------------------------
# Full chain — one ad end to end
# ---------------------------------------------------------------------------
def create_single_image_ad(account_id: str, campaign_id: str, image_path: str,
                           intro_text: str, headline: str, destination_url: str,
                           call_to_action: str = "LEARN_MORE",
                           owner_urn: Optional[str] = None, status: str = "DRAFT",
                           token: Optional[str] = None) -> dict:
    """Upload image → create dark post → create ad. Returns the IDs created."""
    token = token or get_token()
    owner = owner_urn or resolve_page_for_account(account_id, token)
    image_urn = upload_image(image_path, owner, token)
    post_urn = create_link_post(owner, image_urn, intro_text, headline,
                                destination_url, account_id, token=token)
    creative = create_creative(account_id, campaign_id, post_urn,
                               call_to_action, status, token)
    return {"image_urn": image_urn, "post_urn": post_urn, "creative": creative}


# ---------------------------------------------------------------------------
# Bulk — create many ads from a CSV
# ---------------------------------------------------------------------------
# CSV columns: image_path, intro_text, headline, call_to_action, destination_url
def bulk_create_from_csv(account_id: str, campaign_id: str, csv_path: str,
                         owner_urn: Optional[str] = None, status: str = "DRAFT",
                         token: Optional[str] = None) -> list:
    """Create one single-image ad per CSV row. Returns a per-row result list."""
    owner = owner_urn or DEFAULT_ORG_URN
    token = token or get_token()
    results = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for i, row in enumerate(csv.DictReader(f), 1):
            img = (row.get("image_path") or "").strip()
            if not img:
                continue
            try:
                out = create_single_image_ad(
                    account_id, campaign_id, img,
                    (row.get("intro_text") or "").strip(),
                    (row.get("headline") or "").strip(),
                    (row.get("destination_url") or "").strip(),
                    (row.get("call_to_action") or "LEARN_MORE").strip(),
                    owner, status, token,
                )
                results.append({"row": i, "image": img, "ok": True, **out})
            except Exception as e:  # noqa: BLE001 - report and continue
                results.append({"row": i, "image": img, "ok": False, "error": str(e)})
    return results
