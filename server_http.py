#!/usr/bin/env python3
"""
HTTP entrypoint for the LinkedIn Ads MCP — host on Railway, connect your MCP
client to it remotely (fast network, no local throttle, no MCP timeouts).

Serves (all on one Railway service / one URL):
  - the MCP over streamable-HTTP at  /mcp   (protected by MCP_API_KEY)
  - an open  /health  check (env + token scopes)
  - the Flask scheduling dashboard at  /  (UI, schedule editor, and the
    pause/resume run-history log) — protected by DASHBOARD_USERNAME/PASSWORD.

Run:  uvicorn server_http:app --host 0.0.0.0 --port $PORT

Security:
  - MCP_API_KEY (required): clients call /mcp with `Authorization: Bearer <key>`
    or `?key=<key>`. Without it /mcp returns 401. A public MCP can control ad
    accounts — never deploy without MCP_API_KEY set.
  - DASHBOARD_USERNAME / DASHBOARD_PASSWORD: HTTP Basic Auth for the dashboard.
    The dashboard can pause/resume campaigns, so SET THESE in production (if both
    are empty the dashboard is open).
  - For schedules + history to survive redeploys, attach a Railway volume mounted
    at /data (the dashboard writes /data/schedules.json + /data/scheduler_history.json
    when RAILWAY_ENVIRONMENT is set).
"""
import os
import requests
from starlette.responses import JSONResponse

import linkedin_ads_server as li  # importing registers all MCP tools on li.mcp

mcp = li.mcp
# Stateless = each request is independent (robust on Railway; no sticky sessions)
try:
    mcp.settings.stateless_http = True
    mcp.settings.json_response = True
    mcp.settings.transport_security = None  # allow any Host (Railway domain); our key handles auth
except Exception:
    pass

API_KEY = os.environ.get("MCP_API_KEY", "")
_REQUIRED = ["LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET", "LINKEDIN_ACCESS_TOKEN",
             "LINKEDIN_REFRESH_TOKEN", "LINKEDIN_ORG_URN"]

_mcp_app = mcp.streamable_http_app()  # Starlette app w/ correct session lifespan

# Mount the Flask scheduling dashboard (UI + schedule + run-history logs) on this
# same Railway service. Importing `dashboard` also starts its background weekday
# scheduler thread (auto pause/resume) and points li.SCHEDULES_FILE at /data on
# Railway, so schedules set via the MCP tools and via the dashboard stay in sync.
# Wrapped WSGI->ASGI so it runs under the same uvicorn process as the MCP.
try:
    import dashboard  # noqa: E402  (also starts the background scheduler on import)
    from asgiref.wsgi import WsgiToAsgi
    _dash_asgi = WsgiToAsgi(dashboard.app)
    _dash_error = ""
except Exception as _e:  # a dashboard problem must never take down the MCP
    _dash_asgi = None
    _dash_error = str(_e)


def _health_payload() -> dict:
    keys = _REQUIRED + ["LINKEDIN_API_VERSION", "LINKEDIN_OAUTH_SCOPES", "MCP_API_KEY"]
    env = {k: bool(os.environ.get(k)) for k in keys}
    out = {"env_present": env, "mcp_path": "/mcp", "auth_required": bool(API_KEY)}
    try:
        tok = li.get_credentials()
        ij = requests.post(
            "https://www.linkedin.com/oauth/v2/introspectToken",
            data={"client_id": os.environ.get("LINKEDIN_CLIENT_ID", ""),
                  "client_secret": os.environ.get("LINKEDIN_CLIENT_SECRET", ""),
                  "token": tok}, timeout=20,
        ).json()
        out["token_active"] = ij.get("active")
        out["scopes"] = ij.get("scope", "")
        out["has_w_organization_social"] = "w_organization_social" in ij.get("scope", "")
        out["has_rw_ads"] = "rw_ads" in ij.get("scope", "")
    except Exception as e:
        out["token_error"] = str(e)
    out["READY"] = bool(
        all(env[k] for k in _REQUIRED)
        and out.get("has_w_organization_social")
        and bool(API_KEY)
    )
    return out


async def app(scope, receive, send):
    """ASGI router:
      - /health           -> MCP health JSON (open)
      - /mcp...           -> MCP streamable-HTTP (MCP_API_KEY required)
      - everything else   -> Flask scheduling dashboard (its own Basic Auth)
    Non-HTTP scopes (lifespan, etc.) go to the MCP app so its session manager
    starts correctly."""
    stype = scope.get("type")
    if stype != "http":
        # lifespan / websocket -> MCP app (owns the session-manager lifespan)
        await _mcp_app(scope, receive, send)
        return

    path = scope.get("path", "")

    if path == "/health":
        await JSONResponse(_health_payload())(scope, receive, send)
        return

    if path.startswith("/mcp"):
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"").decode()
        from urllib.parse import parse_qs
        key_q = (parse_qs(scope.get("query_string", b"").decode()).get("key") or [""])[0]
        if not API_KEY or (auth != f"Bearer {API_KEY}" and key_q != API_KEY):
            await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
            return
        await _mcp_app(scope, receive, send)
        return

    # everything else -> the scheduling dashboard (UI, schedules, history log)
    if _dash_asgi is not None:
        await _dash_asgi(scope, receive, send)
    else:
        await JSONResponse(
            {"error": "dashboard unavailable", "detail": _dash_error}, status_code=503
        )(scope, receive, send)
