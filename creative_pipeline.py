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


def _load_dotenv_once() -> None:
    """Best-effort .env loader (repo dir) so the CLI works from a fresh shell.

    Only fills variables that aren't already exported — real env always wins.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except IOError:
        pass


_load_dotenv_once()

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
    if path and not os.path.exists(path):
        # relative token path (e.g. ./linkedin_token.json): also try repo dir
        alt = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
        if os.path.exists(alt):
            path = alt
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
# Step 2b — create the sponsored (dark) IMAGE post for lead-gen ads
# ---------------------------------------------------------------------------
def create_image_post(owner_urn: str, image_urn: str, intro_text: str,
                      headline: str, account_id: str,
                      token: Optional[str] = None) -> str:
    """Create a Direct Sponsored Content image post (no article link).

    Used for LEAD_GENERATION ads: the Lead Gen Form (set on the creative) is
    the click destination, so the post carries only the image + copy. The
    headline rides on content.media.title.
    Returns the post URN (urn:li:share:... or urn:li:ugcPost:...).
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
            "media": {
                "title": headline,
                "id": image_urn,
            }
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": True,
    }
    resp = requests.post(f"{API_BASE}/posts", headers=_headers(token), json=body, timeout=60)
    if resp.status_code >= 400:
        raise RuntimeError(f"create image post failed ({resp.status_code}): {resp.text}")
    post_urn = _created_urn(resp)
    if not post_urn:
        raise RuntimeError(f"create image post returned no x-restli-id. Body: {resp.text}")
    return post_urn


# ---------------------------------------------------------------------------
# Lead Gen Forms — list / resolve
# ---------------------------------------------------------------------------
def _form_name(form: dict) -> str:
    """Best-effort human name from a leadForms element (name may be localized)."""
    name = form.get("name", "")
    if isinstance(name, dict):
        localized = name.get("localized") or {}
        if isinstance(localized, dict) and localized:
            return str(next(iter(localized.values())))
        return str(name.get("value", "") or name)
    return str(name)


def list_lead_forms(account_id: str, token: Optional[str] = None) -> list:
    """Return the account's Lead Gen Forms as [{id, urn, name, state}].

    NOTE: /leadForms takes `owner` as a Restli UNION — it must be sent as
    owner=(sponsoredAccount:urn%3Ali%3AsponsoredAccount%3A<id>), not as a bare
    URN (a bare URN 400s with "union type is not backed by a DataMap").
    """
    from urllib.parse import quote
    urn = f"urn:li:sponsoredAccount:{account_id}"
    query = f"q=owner&owner=(sponsoredAccount:{quote(urn, safe='')})&count=100"
    resp = requests.get(f"{API_BASE}/leadForms?{query}", headers=_headers(token), timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"list leadForms failed ({resp.status_code}): {resp.text}")
    out = []
    for el in resp.json().get("elements", []):
        fid = str(el.get("id", ""))
        out.append({
            "id": fid,
            "urn": fid if fid.startswith("urn:") else f"urn:li:adForm:{fid}",
            "name": _form_name(el),
            "state": el.get("state", el.get("status", "")),
        })
    return out


def resolve_lead_form(account_id: str, lead_form: str,
                      token: Optional[str] = None) -> str:
    """Turn a lead form reference (URN, bare ID, or name substring) into an adForm URN."""
    s = str(lead_form).strip()
    if s.startswith("urn:"):
        return s
    if s.isdigit():
        return f"urn:li:adForm:{s}"
    forms = list_lead_forms(account_id, token)
    matches = [f for f in forms if s.lower() in f["name"].lower()]
    if len(matches) == 1:
        return matches[0]["urn"]
    names = ", ".join(f'"{f["name"]}" ({f["id"]})' for f in forms) or "(none found)"
    kind = "Ambiguous" if matches else "No"
    raise ValueError(
        f'{kind} lead form match for "{s}" on account {account_id}. '
        f"Available forms: {names}. Pass the numeric form ID instead."
    )


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
# Step 3b — create a LEAD GEN ad (creative + leadgenCallToAction -> form)
# ---------------------------------------------------------------------------
def create_leadgen_creative(account_id: str, campaign_id: str, post_urn: str,
                            lead_form_urn: str, call_to_action: str = "DOWNLOAD",
                            status: str = "DRAFT", token: Optional[str] = None) -> str:
    """Create a lead-gen ad under a LEAD_GENERATION campaign.

    The creative carries leadgenCallToAction pointing at the Lead Gen Form —
    that (not a URL) is the click destination. Tries the documented
    {"destination": ...} key first and falls back to {"destinationForm": ...}
    if the API version rejects the field name (self-heals across versions).
    """
    cta = call_to_action.upper().strip() or "DOWNLOAD"
    if cta not in VALID_CTAS:
        cta = "LEARN_MORE"

    def _body(dest_key: str) -> dict:
        return {
            "campaign": f"urn:li:sponsoredCampaign:{campaign_id}",
            "intendedStatus": status.upper(),
            "content": {"reference": post_urn},
            "leadgenCallToAction": {dest_key: lead_form_urn, "label": cta},
        }

    last_err = ""
    for dest_key in ("destination", "destinationForm"):
        resp = requests.post(
            f"{API_BASE}/adAccounts/{account_id}/creatives",
            headers=_headers(token), json=_body(dest_key), timeout=60,
        )
        if resp.status_code < 400:
            return _created_urn(resp) or resp.json().get("id", "unknown")
        last_err = f"({resp.status_code}): {resp.text}"
        # Only retry with the alternate key if the complaint is about the field
        if not any(k in resp.text for k in ("destination", "leadgenCallToAction", "UNRECOGNIZED", "unrecognized")):
            break
    raise RuntimeError(f"create leadgen creative failed {last_err}")


def create_lead_gen_image_ad(account_id: str, campaign_id: str, image_path: str,
                             intro_text: str, headline: str, lead_form: str,
                             call_to_action: str = "DOWNLOAD",
                             owner_urn: Optional[str] = None, status: str = "DRAFT",
                             token: Optional[str] = None) -> dict:
    """Upload image → create dark IMAGE post → create lead-gen ad tied to a form.

    lead_form accepts a urn:li:adForm:... URN, a bare numeric form ID, or a
    form-name substring (resolved via list_lead_forms).
    """
    token = token or get_token()
    owner = owner_urn or resolve_page_for_account(account_id, token)
    form_urn = resolve_lead_form(account_id, lead_form, token)
    image_urn = upload_image(image_path, owner, token)
    post_urn = create_image_post(owner, image_urn, intro_text, headline,
                                 account_id, token=token)
    creative = create_leadgen_creative(account_id, campaign_id, post_urn,
                                       form_urn, call_to_action, status, token)
    return {"image_urn": image_urn, "post_urn": post_urn,
            "creative": creative, "lead_form": form_urn}


def create_bare_image_ad(account_id: str, campaign_id: str, image_path: str,
                         intro_text: str, headline: str,
                         owner_urn: Optional[str] = None, status: str = "DRAFT",
                         token: Optional[str] = None) -> dict:
    """Upload image → dark IMAGE post → plain creative, NO form and NO URL.

    For LEAD_GENERATION ad sets when you want to draft the creative now and
    attach the Lead Gen Form manually in Campaign Manager before launch.
    """
    token = token or get_token()
    owner = owner_urn or resolve_page_for_account(account_id, token)
    image_urn = upload_image(image_path, owner, token)
    post_urn = create_image_post(owner, image_urn, intro_text, headline,
                                 account_id, token=token)
    creative = create_creative(account_id, campaign_id, post_urn,
                               status=status, token=token)
    return {"image_urn": image_urn, "post_urn": post_urn, "creative": creative}


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
#   Optional per-row column for LEAD_GENERATION campaigns: lead_form
#   (adForm URN, numeric form ID, or form-name substring). When lead_form is
#   set (or default_lead_form is passed), the row becomes a lead-gen ad and
#   destination_url is ignored.
def bulk_create_from_csv(account_id: str, campaign_id: str, csv_path: str,
                         owner_urn: Optional[str] = None, status: str = "DRAFT",
                         token: Optional[str] = None,
                         default_lead_form: Optional[str] = None,
                         formless: bool = False) -> list:
    """Create one single-image ad per CSV row. Returns a per-row result list.

    formless=True: build every row as a bare image ad (no URL, no form) —
    for drafting into LEAD_GENERATION ad sets and attaching forms in the UI.
    """
    owner = owner_urn or DEFAULT_ORG_URN
    token = token or get_token()
    results = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for i, row in enumerate(csv.DictReader(f), 1):
            img = (row.get("image_path") or "").strip()
            if not img:
                continue
            lead_form = (row.get("lead_form") or "").strip()
            dest = (row.get("destination_url") or "").strip()
            if not lead_form and not dest:
                # default form only fills rows that don't declare a URL,
                # so mixed CSVs keep their link rows as link ads
                lead_form = (default_lead_form or "").strip()
            try:
                if formless:
                    out = create_bare_image_ad(
                        account_id, campaign_id, img,
                        (row.get("intro_text") or "").strip(),
                        (row.get("headline") or "").strip(),
                        owner, status, token,
                    )
                elif lead_form:
                    out = create_lead_gen_image_ad(
                        account_id, campaign_id, img,
                        (row.get("intro_text") or "").strip(),
                        (row.get("headline") or "").strip(),
                        lead_form,
                        (row.get("call_to_action") or "DOWNLOAD").strip(),
                        owner, status, token,
                    )
                elif not dest:
                    raise ValueError(
                        "Row has neither destination_url nor lead_form — set one "
                        "(lead_form for LEAD_GENERATION ad sets, destination_url otherwise)."
                    )
                else:
                    out = create_single_image_ad(
                        account_id, campaign_id, img,
                        (row.get("intro_text") or "").strip(),
                        (row.get("headline") or "").strip(),
                        dest,
                        (row.get("call_to_action") or "LEARN_MORE").strip(),
                        owner, status, token,
                    )
                results.append({"row": i, "image": img, "ok": True, **out})
            except Exception as e:  # noqa: BLE001 - report and continue
                results.append({"row": i, "image": img, "ok": False, "error": str(e)})
    return results
