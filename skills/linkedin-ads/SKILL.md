---
name: linkedin-ads
description: >-
  Create and manage LinkedIn ads end-to-end through the linkedin-ads MCP server —
  upload image creatives, build sponsored (dark) posts, create single-image ads,
  duplicate ads/campaigns, write targeting, set budgets/bids, and pull analytics,
  across one or many ad accounts. Use whenever the user wants to build, launch,
  duplicate, or manage LinkedIn ads/campaigns, push creatives to a LinkedIn ad
  account, or run a LinkedIn ad funnel. The user typically hands over image files
  + ad copy and asks the assistant to "create the ads."
---

# LinkedIn Ads — build & manage

This skill drives the **linkedin-ads MCP server** (Python, `/rest` API v202605).
The server provides the capability; this file is the operating playbook so ad
creation works on the first try.

## What the tools can do
- **Creatives (build):** `upload_image`, `create_single_image_ad`,
  `bulk_create_single_image_ads`, `duplicate_ad`
- **Structure:** `create_campaign_group`, `create_campaign`, `duplicate_campaign`,
  `update_campaign`, `schedule_campaign`, `pause_resume_campaign`, `set_campaign_budget`,
  `set_bid_strategy`
- **Targeting:** `get_targeting_facets`, `get_targeting_entities`,
  `estimate_audience_size`, `set_campaign_targeting`, `create_saved_audience`
- **Discovery:** `list_accounts`, `list_pages`, `resolve_page_for_account`,
  `list_campaigns`, `list_creatives`, `list_lead_forms`, `list_conversions`
- **Analytics:** `get_campaign_analytics`, `get_account_analytics`,
  `get_creative_analytics`, `get_demographic_analytics`, etc.

## How a single-image ad is actually built (the chain)
A LinkedIn ad references a **post**, which references an **image**. `create_single_image_ad`
does all three steps; do them manually only when debugging:
1. **Upload image** → `urn:li:image:…` (owner must be the **organization/Page** URN).
2. **Create a DSC dark post** → `urn:li:share:…` (includes `adContext.dscAdAccount`,
   `feedDistribution: NONE`, `content.article` = {source URL, title=headline, thumbnail=image}).
3. **Create the creative** in the campaign (ad set) → `urn:li:sponsoredCreative:…`,
   body = `{campaign, intendedStatus, content:{reference: post}}`.

## Hard rules (these caused real failures — respect them)
- **New creatives must be created as `DRAFT`** (or `ACTIVE` → goes to review). You
  **cannot** create one as `PAUSED` (PAUSED is only allowed after review = APPROVED).
  Default to DRAFT, then flip to ACTIVE/PAUSED later.
- **Creative body must NOT include** a top-level `status` or `content.callToAction`
  (current API rejects both with 422). The click-through comes from the post's article link.
- **Image owner / post author = the organization (Page) URN**, never a person — company
  ads must be Page-authored.
- **Required scopes:** `rw_ads` **and** `w_organization_social` (plus `r_organization_admin`).
  The authorizing member must be ADMIN / DSC-poster / CONTENT_ADMIN of the Page.
  No Community Management API product is needed if the app is verified.
- **Images are auto-compressed** before upload (Pillow). Big PNGs otherwise upload
  very slowly. Keep source creatives reasonable; the pipeline handles the rest.

## Multi-account / multi-page
- Each tool takes `account_id` and an org/owner URN per call — nothing is hardcoded.
- `account_pages.json` maps `{account_id: org_urn}`; `resolve_page_for_account`
  auto-picks the Page so you don't pass it each time. Use `list_pages` to enumerate
  Pages you administer, `list_accounts` for ad accounts.

## Typical workflow (what the user wants)
1. User gives **image files + copy** (intro, headline, destination URL, CTA) and the
   target **account + campaign (ad set)**.
2. Resolve the Page for the account (or use the provided org URN).
3. Create each ad with `create_single_image_ad` (status **DRAFT**), or
   `bulk_create_single_image_ads` from a CSV
   (`image_path,intro_text,headline,call_to_action,destination_url`).
4. Report the created `image / post / creative` URNs.
5. Activate later (after LinkedIn review) or leave as DRAFT for the user to launch.

To **scale an existing ad** across ad sets/accounts, use `duplicate_ad` (it references
an existing approved post, so it works with `rw_ads` alone).

## CTAs (valid values)
`LEARN_MORE, SIGN_UP, DOWNLOAD, REGISTER, REQUEST_DEMO, SUBSCRIBE, APPLY, JOIN,
ATTEND, GET_QUOTE, VIEW_QUOTE, SEE_MORE`. Map invalid ones (e.g. "Request Assessment")
to the closest valid CTA.

## Known reference IDs (Framework Security)
- Ad account: `507196009`  ·  Page/org: `urn:li:organization:40686922`
- Construction TOFU ad set used in testing: `799010234`
- Construction landing page: `https://frameworksecurity.com/construction-cybersecurity`

## Naming conventions
- Campaign groups / campaigns: `[VERT] | [STAGE] | [Theme] | [YYYY-MM]`
  (VERT = CON/FIN, STAGE = TOF/MOF/BOF).
- Ad (content) name: `Linkedin Ad Creative_Testing{N}_{Concept}_/{destination}`.

## Health / troubleshooting
- The dashboard exposes `/health` — it introspects the token and shows granted
  scopes + `READY`. Hit it after any token/scope change.
- `initializeUpload` 400 "Organization permissions..." → token missing
  `w_organization_social` (re-auth with that scope).
- 422 about `callToAction` / `status` → creative body has disallowed fields.
- 400 "transition … null to PAUSED" → create as DRAFT instead.
- Slow uploads / MCP timeouts → image too large or throttled connection (compression
  fixes this; or run server-side on Railway).

## CLI alternative
`python bulk_create_ads.py --account <id> --campaign <id> --csv <file> --status DRAFT`
runs the same pipeline without the MCP timeout (good for big batches).

## Field notes — verified live (June 2026)

**Authoritative docs (consult when unsure):** https://learn.microsoft.com/en-us/linkedin/marketing/integrations/ads/advertising-targeting/ads-targeting — and the sibling pages under .../integrations/ads/ (create-and-manage-campaigns, image-ads-integrations). Always match the current `li-lms-YYYY-MM` version.

**Creating a campaign GROUP** requires a `runSchedule` → pass a `start_date` (else 422 "runSchedule field is required").

**Creating a CAMPAIGN** requires these now-mandatory fields (the server sends them by default):
- `offsiteDeliveryEnabled: false` — and it **must be false for LEAD_GENERATION** (can't be true with Lead Gen).
- `politicalIntent`: enum `NOT_POLITICAL` (B2B), `POLITICAL`, or `NOT_DECLARED`.
- Lead Gen document ad type = objective `LEAD_GENERATION`, campaign type `SPONSORED_UPDATES`; the document + lead form are attached to the ad in the UI.

**Dev-Tier account access:** the app can READ all accounts, but to CREATE/manage in an account it must be added to the app's **Account Management list** (LinkedIn app → Products → Advertising API → View Ad Accounts). Otherwise writes 403 "application is not configured to access the related advertiser account(s)."

**Targeting entity resolution (`/adTargetingEntities`)** — two finders, pick by facet:
- `q=typeahead&facet={urn}&query={text}` → titles, industries, locations, skills, employers, schools (search).
- `q=adTargetingFacet&facet={urn}` (no query) → seniorities, staffCountRanges, jobFunctions, genders, ageRanges (returns all).
- Param is `facet` (URL-encoded URN), NOT `adTargetingFacet`. Optional `queryVersion=QUERY_USES_URNS`, `locale=(language:en,country:US)`.
- Locations are typeahead-only now (Bing geo migration).

**targetingCriteria shape** (for set_campaign_targeting / campaign create):
`{"include":{"and":[{"or":{"<facetUrn>":["<entityUrn>", ...]}}, ...]}, "exclude":{"or":{"<facetUrn>":[...]}}}`
Company size 200–10,000 = staffCountRange entities for 201-500, 501-1000, 1001-5000, 5001-10000.

**New creatives must be DRAFT first** (can't create as PAUSED until reviewStatus=APPROVED). Creative body = `{campaign, intendedStatus, content:{reference}}` — no top-level `status`, no `content.callToAction`.

**Image upload (org-owned)** needs `w_organization_social` (member must be Page ADMIN/DSC_POSTER/CONTENT_ADMIN). A verified Advertising-API app grants it — no Community Management API required. Compress large images before upload.

**Hosting (Railway HTTP MCP):** `server_http.py` serves `/mcp` (streamable-http) + `/health`. Must set `mcp.settings.transport_security = None` (else 421 "Invalid Host header" on the Railway domain). Auth via `MCP_API_KEY` as `Authorization: Bearer` header OR `?key=` URL param (Claude connectors use the URL param). Reference: account 507196009 / 513217390 (Squid AI) / org urn:li:organization:40686922.

## Field notes — targeting URN formats (verified live, June 2026)

**Range facets use NUMERIC TUPLE URNs, not SIZE_ enums.** This is the #1 gotcha. Company size = `urn:li:staffCountRange:(201,500)`, `(501,1000)`, `(1001,5000)`, `(5001,10000)`, `(1,1)`, `(2,10)`, `(11,50)`, `(51,200)`, `(10001,2147483647)`. **NOT** `SIZE_201_TO_500` and **NOT** `organizationStaffCountRange` — both 400 with `INVALID_VALUE_FOR_FIELD`. Same tuple pattern for `ageRanges` = `urn:li:ageRange:(25,34)`, `growthRate` = `urn:li:growthRate:(3,10)`, `yearsOfExperience` = `urn:li:yearsOfExperience:N`. `2147483647` (INT_MAX) = "no upper limit."

**ALWAYS target permanent location, never "recent or permanent."** Use `urn:li:adTargetingFacet:profileLocations` (matches the member's *profile* location only = permanent). Do **not** use `urn:li:adTargetingFacet:locations` — that one matches *either* current IP/recent location *or* profile location. Both facets take the same `urn:li:geo:…` value URNs; just swap the facet key. (House rule for all campaigns.)

**Range-facet resolution — FIXED & DEPLOYED (June 2026).** `get_targeting_entities` now resolves range facets (`staffCountRanges`, `seniorities`, `ageRanges`, `jobFunctions`) correctly. Root cause was that the `adTargetingFacet`/`typeahead` finders default to `queryVersion=QUERY_USES_MIXED`, which returns a legacy `{"value":{"string":"urn:…"}}` blob instead of `urn`/`name`/`facetUrn`; the tool now sends `queryVersion=QUERY_USES_URNS` and falls back to parsing `value.string`. You can resolve company sizes/seniorities live again — but the documented tuple URNs above are still the source of truth for the value FORMAT.

**Authoritative value-URN list:** https://learn.microsoft.com/en-us/linkedin/shared/references/v2/ads/targeting-criteria-facet-urns — append `?accept=text/markdown` to fetch the light/fast version (the full HTML page times out). It lists the exact value URN for every facet.

**set_campaign_targeting** PATCHes the whole `targetingCriteria` atomically (a bad URN rejects the entire patch, so the campaign never ends up half-set — safe to iterate on a DRAFT). The deployed tool auto-appends an `interfaceLocales=[urn:li:locale:en_US]` clause; that's expected, not an error.

**estimate_audience_size — FIXED & DEPLOYED (June 2026).** Pass a normal targetingCriteria JSON string; the tool now calls `/audienceCounts` with the RestLi-2.0 finder `q=targetingCriteriaV2` and the criteria RestLi-encoded internally (URN chars percent-encoded, structural `():,List(` literal), no `account` param. Returns `total` and `active` counts; `total` is 0 when the true audience is under LinkedIn's 300 minimum. Use it freely to size targeting before launch. (You can also still validate by PATCHing a DRAFT via `set_campaign_targeting` + `get_campaign_details`.)

**AND-clause restrictions (LinkedIn rules):** `staffCountRanges` and `industries` may NOT be AND'ed with an include clause targeting Employers. `seniorities` and `jobFunctions` may NOT be AND'ed with an include clause targeting `titles` — so when targeting by Job Titles, do NOT also add a seniority/jobFunction AND-clause (it'll reject). `ageRanges`, `genders`, `groups`, `interfaceLocales` are include-only.

## Resolving job titles to the ICP (not literal strings)

LinkedIn only targets **standardized** titles, so a client's wish-list rarely maps 1:1. Treat the supplied list as a description of the **ICP role**, not exact strings to match. Never invent a `urn:li:title:` — if a title can't be resolved, find an equivalent or drop it.

**First, lock the ICP in your head** before resolving — three axes:
1. **Function / domain** — what the person owns (e.g. customer support, technical support, service delivery, customer experience, technical operations, customer engineering, escalation).
2. **Seniority band** — the floor the client wants (e.g. Director and above: Director, Senior Director, Head of, VP, SVP, Chief). Don't drift below it.
3. **Org context** — already handled by the industry + company-size + location facets; titles only need to nail function × seniority.

**Resolution loop, per requested title:**
1. `get_targeting_entities(facet="urn:li:adTargetingFacet:titles", query=...)` with the title as written.
2. If empty, **reword toward how LinkedIn standardizes titles** and retry: expand abbreviations (`VP`→`Vice President`, `SVP`→`Senior Vice President`, `Sr`→`Senior`), drop filler ("of", "Global", "Lead/Leader"→"Manager"/"Director"), or try the function alone ("Escalation Management" → "Escalation Manager"). 2-3 rewordings is plenty.
3. From the candidates, **keep the match whose function AND seniority both fit the ICP.** A same-function title one notch off in wording is fine; a title that drops below the seniority floor (e.g. a "Manager" standing in for a "Director") is **not** — drop it instead, unless the client explicitly listed Manager-level (TicketMind did list "Technical Support Manager").
4. If nothing fits, drop it. Better a tight list of real URNs than a padded one that dilutes the audience or skews junior.
5. **Dedup** URNs across the whole list, and **report substitutions + drops** so the client can sanity-check (e.g. "‘Global Support Leader’ → no standardized equivalent, dropped; ‘Director IT Service Management’ → none, dropped").

**Sense-check the result** with `estimate_audience_size` — if the audience is implausibly small (<~50k for sponsored content, or under the 300 floor), the title list is probably too narrow or a seniority/function AND-clause is fighting the titles; loosen wording or add adjacent functions before launch.

**When a title list is too narrow (or you had to drop a lot), run a TITLE EXPANSION SWEEP** before falling back to function+seniority — it often closes the gap while keeping title-level precision:
1. Take the ICP's function clusters and sweep typeahead across **synonym families**, not just the client's exact words. For a support/CX ICP that means: *support, technical support, customer support, customer service, customer care, customer success, customer experience, customer operations, service delivery, technical services, escalation*. Spell out seniority (`Vice President`, `Senior Director`, `Head of`, `Chief … Officer`) and try the noun alone (`Escalation Manager`).
2. **Keep** matches that hold the ICP seniority floor (Director / Head / VP / C-level). **Skip** IC/junior homonyms the search also returns (`Customer Success Manager`, `Customer Service Manager`, `…Center Manager`) unless the client explicitly wants Manager-level. **Skip off-ICP homonyms** — e.g. *Chief Employee Experience Officer* is HR, not customer experience; *Customer Operations* can be broader than support.
3. Dedup against the list you already have, then re-run `estimate_audience_size` to confirm the lift.

This is genuinely effective: it captures people whose real title (e.g. "SVP Support") has no standardized URN but who *do* standardize under an adjacent name (VP Customer Service, Head of Customer Success, Director Technical Support, etc.).

**Reference — TicketMind ICP (Squid AI):** customer-support & CX **leadership (Director → C-level)** in *support, technical support, customer support, service delivery, customer experience, technical operations, technical services, customer engineering, escalation* functions, at 201–10,000-employee tech / fintech / healthcare / telecom companies in the US & Canada. Initial literal resolution gave 19 title URNs (~34k audience); titles like "SVP Support", "Senior Director of Support", "Global Support Leader", "Director IT Service Management", "Head of Customer Engineering", "Director Solutions Support", "Support Engineering Leader", "Technical Services Leader", "Director Escalation Management" had no standardized equivalent. A title-expansion sweep then added 14 adjacent standardized titles — Director/VP/Head of Customer Success, Chief Experience Officer, VP/Director/Head of Customer Service, VP Customer Care, VP/Director Customer Service Operations, Director Technical Support, VP Operations Support, Director Customer Operations, Head of Technical Services — taking the **same precise title approach from 34k → 94k** (LinkedIn's healthy 50k+ zone), without resorting to function+seniority (~130k). Decision: applied the expanded 33-title set to campaign 858290456.

## Known reference IDs (TicketMind / Squid AI)
- Account `513217390` · Campaign group `1184600156` (TicketMind). Active campaign renamed in-UI to **"TOF | Lead Generation | Field 12 Question Guide | Jun 22, 2026"** (the original API-created campaign `858290456` was recreated in the UI, so re-fetch the live ID with `list_campaigns(account_id=513217390, campaign_group_id=1184600156)` rather than trusting the old ID).
- Final targeting: US (`urn:li:geo:103644278`) + Canada (`urn:li:geo:101174742`) via **profileLocations**; company size 201–10,000; 10 tech/fintech/healthcare industries; **33** support/CX leadership titles → **~94k audience**. Bid: **Manual CPC** (per Artur's standing preference — NOT Max Delivery). Delivery: **weekday-only** (paused weekends) for budget efficiency.

## Resolved facet ID reference (so you don't re-resolve)
- **Seniorities** (`urn:li:seniority:N`): 1 Unpaid · 2 Training · 3 Entry · 4 Senior · 5 Manager · 6 Director · 7 VP · 8 CXO · 9 Partner · 10 Owner. "Director and up" = 6,7,8,9,10.
- **Job functions** (`urn:li:function:N`): Customer Success and Support = **26**; Information Technology = 13; Operations = 18; Engineering = 8; Sales = 25 (full list via `get_targeting_entities(facet="urn:li:adTargetingFacet:jobFunctions")`).
- **Locations**: United States = `urn:li:geo:103644278` · Canada = `urn:li:geo:101174742`.
- **TicketMind industries** (`urn:li:industry:N`): Software Development 4 · IT Services & IT Consulting 96 · Computer & Network Security 118 · Telecommunications 8 · Technology, Information & Internet 6 · Computer Networking Products 5 · Financial Services 43 · Data Infrastructure & Analytics 2458 · Hospitals & Health Care 14 · Internet Marketplace Platforms 1285. (Cloud Computing / IoT / Managed Services have NO standardized industry — covered by Software Dev / IT Services / Tech-Info-Internet.)

## Discovery (find IDs before duplicating / analyzing)
- `list_campaigns(account_id, campaign_group_id=…)` and `list_campaign_groups(account_id)` — fixed June 2026 to use the account-scoped path (the old version 400'd with `FIELD_INVALID "search/account"` because it put `account` in the search criteria; account belongs in the PATH, only status/campaignGroup go in `search`).
- Fallback if a list call ever fails: `get_campaign_analytics(account_id, start, end, time_granularity="ALL")` with NO campaign filter returns one row per campaign **with its ID** — a quick way to enumerate campaigns and see headline metrics at once.

## Duplication & scaling
- **`duplicate_ad`** — clones an existing (approved) ad into another campaign/account. Because it references an already-approved post it works with **`rw_ads` alone** (no `w_organization_social`/re-upload). Best way to roll a winning creative across ad sets/accounts.
- **`duplicate_campaign`** — copies a campaign's structure (type, objective, budget, bid, schedule, targetingCriteria, format). Use it to spin up A/B variants, then `set_campaign_targeting` to change just the targeting layer. Example: run the title-list audience as one campaign and the function+seniority audience as a duplicate, and let performance pick the winner.
- Standard launch flow: build/clone as **DRAFT** → set targeting/budget/bid → attach Document + Lead Gen form in the UI (not API-automatable) → activate.

## Analytics & diagnostics playbook
**Tools:** `get_campaign_analytics`, `get_campaign_group_analytics`, `get_account_analytics`, `get_creative_analytics` (funnel: impressions, clicks, CTR, cost, leads, conversions); `get_demographic_analytics` (pivots: `MEMBER_JOB_TITLE`, `MEMBER_JOB_FUNCTION`, `MEMBER_SENIORITY`, `MEMBER_INDUSTRY`, `MEMBER_COMPANY_SIZE`, `MEMBER_COUNTRY_V2`, `MEMBER_REGION_V2`); `get_company_performance` (which companies saw/clicked); `get_lead_gen_performance` (form opens/completions). Use `time_granularity="ALL"` for demographics, `"DAILY"` to see pacing.

**Benchmarks for reading results:** Sponsored Content CTR ≈ 0.4–0.5% (≥0.1% is poor); healthy CPM ≈ $30–80 (a $300–500 CPM means the audience is too small and/or the bid/strategy is forcing spend); minimum audience to run = 300; comfortable Sponsored Content audience ≥ 50k.

**Campaign-diagnosis sequence (why is it underperforming?):**
1. **Funnel** — pull `get_campaign_analytics` ALL: impressions, CTR, CPM, spend, leads. Low impressions + high CPM = delivery choked (small audience and/or Max Delivery).
2. **Seniority leak** — `get_demographic_analytics MEMBER_SENIORITY`: if most impressions are seniority 4–5 (Senior/Manager ICs) the targeting let in non-buyers; tighten to titles or Director+ (6–10).
3. **Reach/efficiency** — check company-size + industry pivots are on-ICP; check the bid strategy (Max Delivery on a thin audience wrecks CPM — prefer Manual CPC).
4. **Creative** — if the *right* people saw it but CTR is ~0.1% and landingPageClicks ≈ 0, it's a creative/offer problem, not targeting.
5. **Age of campaign** — pull DAILY: a campaign only a few days old is still in learning; don't over-read it.
- **No hour-of-day reporting exists** on LinkedIn (API or UI) — daily is the finest granularity. Don't promise dayparting analytics; the connector's weekday scheduler handles day-level pause/resume instead.

## Bidding (Artur's preference)
Default to **Manual CPC**, not Maximize Delivery. Max Delivery spends the full budget regardless and bids itself up against a narrow audience (observed ~$461 CPM, 1 lead on the old "Field Guide" campaign). Manual CPC caps cost-per-click and forces efficient delivery; pair it with a properly sized audience (≥50k). Within manual, start near the low end of LinkedIn's recommended CPC range and raise only if delivery is starved. Do NOT recommend Max Delivery as a fix for poor performance.

## Access tiers / applying for Standard
The app is on the Advertising API **Development tier**: read unlimited accounts, but **edit only up to 5** ad accounts and each must be added to the app's Account-Management allowlist (Developer Portal → app → Products → Advertising API → View Ad Accounts). **Standard tier** removes the 5-account cap and the allowlist (edit unlimited accounts). To upgrade: build & test on Dev tier (done) → record a screen-recording showing the platform create/edit/optimize campaigns → submit a Standard-tier upgrade request as a **support ticket on the LinkedIn Developer Support Portal** (https://www.linkedin.com/help/linkedin/ask/dsapi). LinkedIn reviews case-by-case with no fixed SLA. Docs: https://learn.microsoft.com/en-us/linkedin/marketing/increasing-access

## Hosting + dashboard (one Railway service)
`server_http.py` runs both on `https://linkedin-ads-mcp-production-6593.up.railway.app`: `/mcp` (connector, `MCP_API_KEY`), `/health` (status JSON), and the **Flask scheduling dashboard at `/`** (UI + schedule editor + pause/resume run-history log at `/api/scheduler/history`), wrapped via `asgiref.WsgiToAsgi`. Root "Not Found" only means the dashboard isn't mounted — check `/health`. Protect the dashboard with `DASHBOARD_USERNAME`/`DASHBOARD_PASSWORD`; attach a Railway volume at `/data` so `schedules.json` + `scheduler_history.json` persist across deploys. Schedules set via MCP tools and via the dashboard share the same file.
