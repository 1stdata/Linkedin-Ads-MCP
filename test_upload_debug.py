#!/usr/bin/env python3
"""Isolate the image upload: show the upload host and whether we can connect."""
import requests
from dotenv import load_dotenv
load_dotenv()
import creative_pipeline as cp

OWNER = "urn:li:organization:40686922"
IMG = "/Users/arturmaclellan/Documents/Claude/Projects/Framework Security - LinkedIn/Ads/Linkedin Ad Creative_Testing17.png"

token = cp.get_token()

print("\n[1] initializeUpload (api.linkedin.com) ...", flush=True)
init = requests.post(
    f"{cp.API_BASE}/images?action=initializeUpload",
    headers=cp._headers(token),
    json={"initializeUploadRequest": {"owner": OWNER}},
    timeout=30,
)
print("    status:", init.status_code)
print("    body  :", init.text[:400])
val = init.json().get("value", {})
url = val.get("uploadUrl")
img = val.get("image")
print("    image URN :", img)
print("    uploadUrl :", url)

if not url:
    print("\nNo uploadUrl returned — stopping.")
    raise SystemExit

from urllib.parse import urlparse
host = urlparse(url).hostname
print(f"\n[2] PUT binary to upload host: {host}  (connect timeout 15s) ...", flush=True)
data = open(IMG, "rb").read()
try:
    r = requests.put(url, headers={"Authorization": f"Bearer {token}"}, data=data, timeout=(15, 120))
    print("    PUT status:", r.status_code)
    print("    PUT body  :", r.text[:300])
    print("\n=== UPLOAD OK ===  image URN:", img)
except Exception as e:
    print("    PUT FAILED:", repr(e))
    print("\n>>> The machine cannot reach the upload host above.")
    print(">>> Likely a VPN/firewall block. Try: turn off VPN, or run from a different network.")
