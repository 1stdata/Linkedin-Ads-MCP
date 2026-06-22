#!/usr/bin/env python3
"""Step-by-step local test of the single-image ad pipeline (no MCP timeout).

Run:  python test_create_ad.py
Creates ONE PAUSED ad. Prints each step with timing so we can see exactly
which call is slow/failing.
"""
import time
import traceback
from dotenv import load_dotenv

load_dotenv()  # pull LINKEDIN_ACCESS_TOKEN / token from .env

import creative_pipeline as cp

OWNER = "urn:li:organization:40686922"
ACCOUNT = "507196009"
CAMPAIGN = "799010234"
IMG = "/Users/arturmaclellan/Documents/Claude/Projects/Framework Security - LinkedIn/Ads/Linkedin Ad Creative_Testing17.png"
INTRO = (
    "You lock the gate every night.\nThe network sits wide open.\n\n"
    "You chain the gate and lock the gear.\nMeanwhile payroll, project files and banking\n"
    "stay open all night — and attackers know it.\n\n"
    "We inspect your office network the way\nyou'd inspect a site — and stay until\n"
    "the gaps are closed. Start with a free assessment."
)
HEAD = "Lock Down the Network Too — Free Security Assessment"
URL = "https://frameworksecurity.com/construction-cybersecurity"


def step(label, fn):
    t = time.time()
    print(f"\n[..] {label} ...", flush=True)
    r = fn()
    print(f"[OK] {label}  ({time.time() - t:.1f}s)  -> {r}", flush=True)
    return r


try:
    img = step("1/3 upload_image", lambda: cp.upload_image(IMG, OWNER))
    post = step("2/3 create_link_post", lambda: cp.create_link_post(OWNER, img, INTRO, HEAD, URL, ACCOUNT))
    cr = step("3/3 create_creative", lambda: cp.create_creative(ACCOUNT, CAMPAIGN, post, "LEARN_MORE", "PAUSED"))
    print("\n=== SUCCESS ===")
    print("image   :", img)
    print("post    :", post)
    print("creative:", cr)
except Exception:
    print("\n=== FAILED — full error below ===")
    traceback.print_exc()
