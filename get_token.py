"""
LinkedIn OAuth 2.0 Token Generator

Run this script to obtain an access token for the LinkedIn Marketing API.
It opens your browser, you authorize the app, and the token is saved to .env.

Usage:
    python get_token.py
"""

import os
import sys
import json
import time
import webbrowser
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler

try:
    from dotenv import load_dotenv, set_key
    load_dotenv()
except ImportError:
    print("python-dotenv not installed. Run: pip install python-dotenv")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("requests not installed. Run: pip install requests")
    sys.exit(1)

# Config
CLIENT_ID = os.environ.get("LINKEDIN_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("LINKEDIN_CLIENT_SECRET", "")
SCOPES = os.environ.get("LINKEDIN_OAUTH_SCOPES", "r_ads_reporting,r_ads,rw_ads,rw_organization_admin,r_organization_admin,r_basicprofile,w_organization_social,r_organization_social")
REDIRECT_URI = "http://localhost:8000/callback"
ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"

# Will be set by the callback handler
authorization_code = None


class CallbackHandler(BaseHTTPRequestHandler):
    """Handle the OAuth redirect callback."""

    def do_GET(self):
        global authorization_code

        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if "code" in params:
            authorization_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""
                <html><body style="font-family: sans-serif; text-align: center; padding-top: 80px;">
                <h1>Authorization successful!</h1>
                <p>You can close this tab and return to the terminal.</p>
                </body></html>
            """)
        elif "error" in params:
            error = params.get("error", ["unknown"])[0]
            desc = params.get("error_description", [""])[0]
            self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(f"""
                <html><body style="font-family: sans-serif; text-align: center; padding-top: 80px;">
                <h1>Authorization failed</h1>
                <p>Error: {error}</p><p>{desc}</p>
                </body></html>
            """.encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Suppress default request logging
        pass


def exchange_code_for_token(code: str) -> dict:
    """Exchange the authorization code for an access token."""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }
    resp = requests.post(TOKEN_URL, data=data)
    if resp.status_code != 200:
        print(f"\nError exchanging code for token: {resp.status_code}")
        print(resp.text)
        sys.exit(1)
    tok = resp.json()
    print("\n=== GRANTED SCOPES ===")
    print(tok.get("scope", "(no scope field returned)"))
    print("======================")
    return tok


def save_token(token_data: dict):
    """Save token to .env file and optionally to a JSON token file."""
    access_token = token_data.get("access_token", "")
    refresh_token = token_data.get("refresh_token", "")
    expires_in = token_data.get("expires_in", 0)

    # Save to .env
    set_key(ENV_FILE, "LINKEDIN_ACCESS_TOKEN", access_token)
    if refresh_token:
        set_key(ENV_FILE, "LINKEDIN_REFRESH_TOKEN", refresh_token)

    # Also save to a JSON token file for the server's auto-refresh
    token_file = os.path.join(os.path.dirname(ENV_FILE), "linkedin_token.json")
    token_json = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": expires_in,
        "expires_at": time.time() + expires_in,
    }
    with open(token_file, "w") as f:
        json.dump(token_json, f, indent=2)

    print(f"\nToken saved to {ENV_FILE}")
    print(f"Token JSON saved to {token_file}")
    if expires_in:
        days = expires_in // 86400
        print(f"Token expires in {days} days")


def main():
    if not CLIENT_ID or not CLIENT_SECRET:
        print("Error: LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET must be set in .env")
        sys.exit(1)

    # Build authorization URL
    scope_str = SCOPES.replace(",", " ")
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": scope_str,
        "state": "linkedin_mcp_auth",
    }
    auth_url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

    print("=" * 60)
    print("LinkedIn OAuth 2.0 Token Generator")
    print("=" * 60)
    print(f"\nOpening browser for authorization...")
    print(f"If the browser doesn't open, visit this URL:\n")
    print(auth_url)
    print(f"\nWaiting for callback on {REDIRECT_URI} ...")

    # Start local server first, then open browser
    server = HTTPServer(("localhost", 8000), CallbackHandler)
    webbrowser.open(auth_url)

    # Wait for the callback (single request)
    while authorization_code is None:
        server.handle_request()

    server.server_close()

    print(f"\nAuthorization code received. Exchanging for token...")

    # Exchange code for token
    token_data = exchange_code_for_token(authorization_code)

    # Save
    save_token(token_data)

    print("\nDone! You can now run the MCP server:")
    print("  python linkedin_ads_server.py")


if __name__ == "__main__":
    main()
