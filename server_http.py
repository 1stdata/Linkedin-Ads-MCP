#!/usr/bin/env python3
"""
HTTP entrypoint for the LinkedIn Ads MCP — host on Railway, connect your MCP
client to it remotely (fast network, no local throttle, no MCP timeouts).

Serves:
  - the MCP over streamable-HTTP at  /mcp   (protected by a bearer key)
  - an open  /health  check (env + token scopes)

Run:  uvicorn server_http:app --host 0.0.0.0 --port $PORT

Security: set MCP_API_KEY in the environment. Clients must send
  Authorization: Bearer <MCP_API_KEY>
on /mcp. Without it, /mcp returns 401. (A public MCP can control ad accounts —
never deploy without MCP_API_KEY set.)
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
except Exception:
    pass

API_KEY = os.environ.get("MCP_API_KEY", "")
_REQUIRED = ["LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET", "LINKEDIN_ACCESS_TOKEN",
             "LINKEDIN_REFRESH_TOKEN", "LINKEDIN_ORG_URN"]

_mcp_app = mcp.streamable_http_app()  # Starlette app w/ correct session lifespan


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
    """ASGI app: /health (open), /mcp (bearer-protected), everything else -> MCP app.
    Non-HTTP scopes (lifespan, etc.) pass straight through so the MCP session
    manager starts correctly."""
    if scope.get("type") == "http":
        path = scope.get("path", "")
        if path == "/health":
            await JSONResponse(_health_payload())(scope, receive, send)
            return
        if path.startswith("/mcp"):
            headers = dict(scope.get("headers") or [])
            auth = headers.get(b"authorization", b"").decode()
            if not API_KEY or auth != f"Bearer {API_KEY}":
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
    await _mcp_app(scope, receive, send)
