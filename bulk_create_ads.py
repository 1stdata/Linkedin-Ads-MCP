#!/usr/bin/env python3
"""
bulk_create_ads.py — create LinkedIn single-image ads in bulk from a CSV.

Usage:
    export LINKEDIN_ACCESS_TOKEN=...        # token with rw_ads scope
    export LINKEDIN_ORG_URN=urn:li:organization:XXXX";  # your Page URN
    python bulk_create_ads.py \
        --account 507196009 \
        --campaign 799010234 \
        --csv bulk_ads_template.csv \
        [--status PAUSED]

CSV columns: image_path, intro_text, headline, call_to_action, destination_url
(call_to_action: LEARN_MORE, REQUEST_DEMO, SIGN_UP, DOWNLOAD, REGISTER, ...)
"""
import argparse
import sys

import creative_pipeline as cp


def main() -> int:
    ap = argparse.ArgumentParser(description="Bulk-create LinkedIn single-image ads from a CSV.")
    ap.add_argument("--account", required=True, help="Ad account ID (numeric)")
    ap.add_argument("--campaign", required=True, help="Campaign / ad set ID (numeric)")
    ap.add_argument("--csv", required=True, help="Path to the ads CSV")
    ap.add_argument("--owner", default=None, help="Organization URN (defaults to LINKEDIN_ORG_URN)")
    ap.add_argument("--status", default="ACTIVE", help="ACTIVE, PAUSED, or DRAFT")
    args = ap.parse_args()

    results = cp.bulk_create_from_csv(
        args.account, args.campaign, args.csv, args.owner, args.status,
    )
    ok = sum(1 for r in results if r.get("ok"))
    for r in results:
        if r.get("ok"):
            print(f"  [OK]  row {r['row']}: {r['image']} -> {r['creative']}")
        else:
            print(f"  [ERR] row {r['row']}: {r['image']} -> {r['error']}")
    print(f"\nDone. {ok}/{len(results)} ads created.")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
