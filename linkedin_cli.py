#!/usr/bin/env python3
"""
linkedin_cli.py — create a LinkedIn campaign (group + campaign + targeting + optional ads)
from a single JSON spec. Runs locally, no MCP timeout.

Usage:
    python linkedin_cli.py <spec.json>

The AI fills the spec from your natural-language request; you run this one command.
See ticketmind.json for the schema. Everything is created as DRAFT unless the spec
says otherwise — nothing spends until you launch it in Campaign Manager.

Document/Lead-Gen-Form ads are added in the UI (not automated). 'ads' here are
single-image ads (uses creative_pipeline).
"""
import json
import sys
import time
from dotenv import load_dotenv
load_dotenv()
import requests
import creative_pipeline as cp

FACET = "urn:li:adTargetingFacet:"


def main(spec_path: str) -> int:
    spec = json.load(open(spec_path))
    acct = str(spec["account_id"])
    acct_urn = f"urn:li:sponsoredAccount:{acct}"
    token = cp.get_token()
    H, B = cp._headers(token), cp.API_BASE

    def get(path, params):
        r = requests.get(B + path, headers=H, params=params, timeout=60)
        r.raise_for_status()
        return r.json()

    def post(path, body):
        r = requests.post(B + path, headers=H, json=body, timeout=90)
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code}: {r.text}")
        return r.headers.get("x-restli-id", r.headers.get("X-RestLi-Id", ""))

    def resolve(facet, query):
        try:
            data = get("/adTargetingEntities",
                       {"q": "adTargetingFacet", "adTargetingFacet": FACET + facet, "count": 10, "query": query})
            els = data.get("elements", [])
            for e in els:
                if e.get("name", "").strip().lower() == query.strip().lower():
                    return e.get("urn")
            return els[0].get("urn") if els else None
        except Exception as e:
            print("   resolve error:", query, e)
            return None

    def resolve_list(facet, queries, label):
        urns = []
        if queries:
            print(f"Resolving {label} ...")
            for q in queries:
                if str(q).startswith("urn:"):
                    urns.append(q); continue
                u = resolve(facet, q)
                print(f"  {'OK  ' if u else 'MISS'} {q}")
                if u:
                    urns.append(u)
        return urns

    t = spec.get("targeting", {})
    and_clauses = []
    geos = resolve_list("locations", t.get("geos", []), "geos")
    if geos:
        and_clauses.append({"or": {FACET + "locations": geos}})
    # company sizes: match by label against the staffCountRanges facet
    sizes = []
    if t.get("company_sizes"):
        print("Resolving company sizes ...")
        data = get("/adTargetingEntities",
                   {"q": "adTargetingFacet", "adTargetingFacet": FACET + "staffCountRanges", "count": 25})
        for e in data.get("elements", []):
            if any(s in e.get("name", "") for s in t["company_sizes"]):
                sizes.append(e.get("urn")); print("  OK ", e.get("name"))
    if sizes:
        and_clauses.append({"or": {FACET + "staffCountRanges": sizes}})
    inds = resolve_list("industries", t.get("industries", []), "industries")
    if inds:
        and_clauses.append({"or": {FACET + "industries": inds}})
    sens = resolve_list("seniorities", t.get("seniorities", []), "seniorities")
    if sens:
        and_clauses.append({"or": {FACET + "seniorities": sens}})
    titles = resolve_list("titles", t.get("titles", []), "titles")
    if titles:
        and_clauses.append({"or": {FACET + "titles": titles}})

    criteria = {"include": {"and": and_clauses}}
    excl = {}
    ex_titles = resolve_list("titles", t.get("exclude_titles", []), "exclude titles")
    if ex_titles:
        excl[FACET + "titles"] = ex_titles
    if excl:
        criteria["exclude"] = {"or": excl}
    print(f"\nTargeting: {len(titles)} titles, {len(inds)} industries, {len(sizes)} sizes, "
          f"{len(geos)} geos, {len(sens)} seniorities")

    # group (idempotent by name)
    g = spec.get("group", {})
    gname = g.get("name", spec["campaign"]["name"])
    group_id = None
    try:
        data = get(f"/adAccounts/{acct}/adCampaignGroups", {"q": "search", "count": 100})
        for el in data.get("elements", []):
            if el.get("name") == gname:
                group_id = str(el.get("id")); print(f"\nReusing group '{gname}' -> {group_id}"); break
    except Exception as e:
        print("\ngroup list error (will create new):", e)
    if not group_id:
        gid = post(f"/adAccounts/{acct}/adCampaignGroups",
                   {"account": acct_urn, "name": gname, "status": g.get("status", "ACTIVE")})
        group_id = gid.split(":")[-1] if gid else None
        print(f"\nCreated group '{gname}' -> {gid}")

    # campaign
    c = spec["campaign"]
    body = {
        "account": acct_urn,
        "campaignGroup": f"urn:li:sponsoredCampaignGroup:{group_id}",
        "name": c["name"],
        "objectiveType": c.get("objective", "LEAD_GENERATION").upper(),
        "type": c.get("type", "SPONSORED_UPDATES").upper(),
        "costType": c.get("cost_type", "CPM").upper(),
        "status": c.get("status", "DRAFT").upper(),
        "locale": c.get("locale", {"country": "US", "language": "en"}),
        "dailyBudget": {"amount": str(c["daily_budget"]), "currencyCode": c.get("currency", "USD")},
        "targetingCriteria": criteria,
        "audienceExpansionEnabled": c.get("audience_expansion", False),
        "runSchedule": {"start": int(time.time() * 1000) + 86400000},
    }
    try:
        cid = post(f"/adAccounts/{acct}/adCampaigns", body)
        print("\n=== CAMPAIGN CREATED ===")
        print("group   :", group_id)
        print("campaign:", cid)
    except Exception as e:
        print("\n=== CAMPAIGN FAILED ===\n", e)
        print("\nBody:\n", json.dumps(body, indent=2)[:1500])
        return 1

    # optional single-image ads
    camp_num = cid.split(":")[-1] if cid else None
    for ad in spec.get("ads", []):
        try:
            out = cp.create_single_image_ad(
                acct, camp_num, ad["image"], ad["intro"], ad["headline"], ad["url"],
                ad.get("cta", "LEARN_MORE"), ad.get("owner_org_urn"), ad.get("status", "DRAFT"))
            print("  ad OK:", out["creative"])
        except Exception as e:
            print("  ad FAILED:", ad.get("image"), e)
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python linkedin_cli.py <spec.json>"); sys.exit(2)
    sys.exit(main(sys.argv[1]))
