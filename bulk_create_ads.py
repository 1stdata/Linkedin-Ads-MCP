#!/usr/bin/env python3
"""
bulk_create_ads.py — create LinkedIn single-image ads in bulk from a CSV.

Usage:
    export LINKEDIN_ACCESS_TOKEN=...        # token with rw_ads scope
    export LINKEDIN_ORG_URN=urn:li:organization:XXXX  # your Page URN
    python bulk_create_ads.py \
        --account 507196009 \
        --campaign 799010234 \
        --csv bulk_ads_template.csv \
        [--status DRAFT] \
        [--lead-form <adForm URN | numeric ID | name substring>]

CSV columns: image_path, intro_text, headline, call_to_action, destination_url
Optional column for LEAD_GENERATION ad sets: lead_form
(call_to_action: LEARN_MORE, REQUEST_DEMO, SIGN_UP, DOWNLOAD, REGISTER, ...)

Lead-gen ads: when a row has lead_form set (or --lead-form is passed as the
default for all rows), the ad is created against that Lead Gen Form and
destination_url is ignored. Use --list-forms to see the account's forms.
"""
import argparse
import sys

import creative_pipeline as cp


def main() -> int:
    ap = argparse.ArgumentParser(description="Bulk-create LinkedIn single-image ads from a CSV.")
    ap.add_argument("--account", required=True, help="Ad account ID (numeric)")
    ap.add_argument("--campaign", help="Campaign / ad set ID (numeric)")
    ap.add_argument("--csv", help="Path to the ads CSV")
    ap.add_argument("--owner", default=None, help="Organization URN (defaults to LINKEDIN_ORG_URN)")
    ap.add_argument("--status", default="ACTIVE", help="ACTIVE, PAUSED, or DRAFT")
    ap.add_argument("--lead-form", default=None,
                    help="Default Lead Gen Form for all rows (URN, numeric ID, or name substring). "
                         "Rows with their own lead_form column override this.")
    ap.add_argument("--list-forms", action="store_true",
                    help="List the account's Lead Gen Forms and exit.")
    ap.add_argument("--formless", action="store_true",
                    help="Draft every ad with image + copy only (no URL, no form) — "
                         "attach Lead Gen Forms manually in Campaign Manager before launch.")
    args = ap.parse_args()

    if args.list_forms:
        for f in cp.list_lead_forms(args.account):
            print(f"  {f['id']:>12}  [{f['state']}]  {f['name']}")
        return 0

    if not args.campaign or not args.csv:
        ap.error("--campaign and --csv are required (unless using --list-forms)")

    results = cp.bulk_create_from_csv(
        args.account, args.campaign, args.csv, args.owner, args.status,
        default_lead_form=args.lead_form, formless=args.formless,
    )
    ok = sum(1 for r in results if r.get("ok"))
    for r in results:
        if r.get("ok"):
            form = f"  form={r['lead_form']}" if r.get("lead_form") else ""
            print(f"  [OK]  row {r['row']}: {r['image']} -> {r['creative']}{form}")
        else:
            print(f"  [ERR] row {r['row']}: {r['image']} -> {r['error']}")
    print(f"\nDone. {ok}/{len(results)} ads created.")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
