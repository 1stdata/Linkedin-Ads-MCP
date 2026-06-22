#!/usr/bin/env python3
"""
Create: TicketMind — Core Prospecting  (Squid AI, account 513217390)
- Campaign group: "TicketMind"
- Campaign: Lead Gen objective, Sponsored Content (document ad type), $175/day, DRAFT
- Targeting: US+Canada, 200-10,000 employees, the title + industry lists below

Runs locally (no MCP timeout). Prints progress and the created IDs.

NOTE: The Document + Lead Gen Form *ad* itself is added in the UI (document-format
ads and lead-form attachment aren't automated yet). This builds the targeted,
ready-to-fill campaign shell as DRAFT — nothing spends.
"""
import json
import time
from dotenv import load_dotenv
load_dotenv()
import requests
import creative_pipeline as cp

ACCT = "513217390"
ACCT_URN = f"urn:li:sponsoredAccount:{ACCT}"
GROUP_NAME = "TicketMind"
CAMPAIGN_NAME = "TicketMind — Core Prospecting"
DAILY, CUR = "175", "USD"
GEOS = ["urn:li:geo:103644278", "urn:li:geo:101174742"]  # United States, Canada
SIZE_MATCH = ["201-500", "501-1,000", "1,001-5,000", "5,001-10,000"]

TITLES = [
    "VP Support", "VP Technical Support", "VP Customer Support", "SVP Support",
    "SVP Customer Support", "Head of Support", "Head of Technical Support",
    "Head of Customer Support", "Senior Director of Support", "Director of Support",
    "Director of Support Operations", "Director Customer Support",
    "Technical Support Manager", "Global Support Leader",
    "VP Service Delivery", "Director Service Delivery", "Head of Service Delivery",
    "VP IT Service Management", "Director IT Service Management", "Head of IT Support",
    "Director IT Support",
    "VP Technical Operations", "Director Technical Operations", "VP Customer Engineering",
    "Director Customer Engineering", "Head of Customer Engineering",
    "Director Solutions Support", "Director Escalation Management",
    "Support Engineering Leader", "Technical Services Leader", "VP Technical Services",
    "Chief Customer Officer", "VP Customer Experience", "Director Customer Experience",
]
INDUSTRY_QUERIES = [
    "Software Development", "IT Services and IT Consulting", "Computer and Network Security",
    "Telecommunications", "Technology, Information and Internet", "Computer Networking Products",
    "Financial Services", "Data Infrastructure and Analytics", "Cloud Computing",
    "Internet of Things", "Hospitals and Health Care", "Internet Marketplace Platforms",
    "Managed Services",
]

token = cp.get_token()
H = cp._headers(token)
B = cp.API_BASE


def api_get(path, params):
    r = requests.get(B + path, headers=H, params=params, timeout=60)
    r.raise_for_status()
    return r.json()


def api_post(path, body):
    r = requests.post(B + path, headers=H, json=body, timeout=90)
    if r.status_code >= 400:
        raise RuntimeError(f"{r.status_code}: {r.text}")
    return r.headers.get("x-restli-id", r.headers.get("X-RestLi-Id", ""))


def resolve(facet, query):
    try:
        data = api_get("/adTargetingEntities",
                       {"q": "adTargetingFacet", "adTargetingFacet": facet, "count": 10, "query": query})
        els = data.get("elements", [])
        if not els:
            return None, None
        for e in els:  # prefer exact name match
            if e.get("name", "").strip().lower() == query.strip().lower():
                return e.get("urn"), e.get("name")
        return els[0].get("urn"), els[0].get("name")
    except Exception as e:
        print("   resolve error:", query, e)
        return None, None


print("Resolving titles ...")
title_urns = []
for t in TITLES:
    u, nm = resolve("urn:li:adTargetingFacet:titles", t)
    print(f"  {'OK  ' if u else 'MISS'} {t}  ->  {nm or ''}")
    if u:
        title_urns.append(u)

print("\nResolving industries ...")
ind_urns = []
for q in INDUSTRY_QUERIES:
    u, nm = resolve("urn:li:adTargetingFacet:industries", q)
    print(f"  {'OK  ' if u else 'MISS'} {q}  ->  {nm or ''}")
    if u:
        ind_urns.append(u)

print("\nResolving company sizes ...")
size_urns = []
try:
    data = api_get("/adTargetingEntities",
                   {"q": "adTargetingFacet", "adTargetingFacet": "urn:li:adTargetingFacet:staffCountRanges", "count": 25})
    for e in data.get("elements", []):
        if any(s in e.get("name", "") for s in SIZE_MATCH):
            size_urns.append(e.get("urn"))
            print("  OK ", e.get("name"), e.get("urn"))
except Exception as e:
    print("  size error:", e)

and_clauses = [{"or": {"urn:li:adTargetingFacet:locations": GEOS}}]
if size_urns:
    and_clauses.append({"or": {"urn:li:adTargetingFacet:staffCountRanges": size_urns}})
if ind_urns:
    and_clauses.append({"or": {"urn:li:adTargetingFacet:industries": ind_urns}})
if title_urns:
    and_clauses.append({"or": {"urn:li:adTargetingFacet:titles": title_urns}})
criteria = {"include": {"and": and_clauses}}
print(f"\nTargeting: {len(title_urns)} titles, {len(ind_urns)} industries, "
      f"{len(size_urns)} sizes, {len(GEOS)} geos")

# --- find or create the campaign group ---
group_id = None
try:
    g = api_get(f"/adAccounts/{ACCT}/adCampaignGroups", {"q": "search", "count": 100})
    for el in g.get("elements", []):
        if el.get("name") == GROUP_NAME:
            group_id = str(el.get("id"))
            print(f"\nReusing existing group '{GROUP_NAME}' -> {group_id}")
            break
except Exception as e:
    print("\ngroup list error (will create new):", e)

if not group_id:
    gid = api_post(f"/adAccounts/{ACCT}/adCampaignGroups",
                   {"account": ACCT_URN, "name": GROUP_NAME, "status": "ACTIVE"})
    group_id = gid.split(":")[-1] if gid else None
    print(f"\nCreated group -> {gid}")

# --- create the campaign (DRAFT) ---
camp_body = {
    "account": ACCT_URN,
    "campaignGroup": f"urn:li:sponsoredCampaignGroup:{group_id}",
    "name": CAMPAIGN_NAME,
    "objectiveType": "LEAD_GENERATION",
    "type": "SPONSORED_UPDATES",
    "costType": "CPM",
    "status": "DRAFT",
    "locale": {"country": "US", "language": "en"},
    "dailyBudget": {"amount": DAILY, "currencyCode": CUR},
    "targetingCriteria": criteria,
    "audienceExpansionEnabled": False,
    "runSchedule": {"start": int(time.time() * 1000) + 86400000},
}
try:
    cid = api_post(f"/adAccounts/{ACCT}/adCampaigns", camp_body)
    print("\n=== CAMPAIGN CREATED (DRAFT) ===")
    print("group   :", group_id)
    print("campaign:", cid)
    print("\nNext: add the Document + Lead Gen Form ad to this campaign in Campaign Manager.")
except Exception as e:
    print("\n=== CAMPAIGN FAILED ===")
    print(e)
    print("\nBody used:\n", json.dumps(camp_body, indent=2)[:1500])
