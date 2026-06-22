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
