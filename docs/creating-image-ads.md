# Creating single-image ads through the MCP

**The one rule:** the LinkedIn Ads MCP runs on **Railway (remote)**, so any tool that
takes an `image_path` or `csv_path` (`create_single_image_ad`, `bulk_create_single_image_ads`,
`upload_image`) reads that path from the **server's own filesystem** — never from your Mac.

If you hand it a local path or a URL, it fails:

```
[Errno 2] No such file or directory: '/Users/you/....png'        # your Mac — server can't see it
[Errno 2] No such file or directory: '/sessions/.../....png'     # sandbox — server can't see it
[Errno 2] No such file or directory: 'https://.../image.png'     # the code does open(path), it does NOT download URLs
```

So the images (and the CSV) have to be **on the server**. The only way they get there is by
being **committed to the repo and deployed** — Railway rebuilds the container from the repo on
every push, and `creatives/` is read as a plain relative path (`./creatives/x.png`) from the app
directory. It is **not** on the `/data` volume (that volume only holds the token, `schedules.json`,
and bid-pacing state).

## The method that works every time

1. Put the image files in the repo's **`creatives/`** folder (it is NOT gitignored).
2. Put a CSV at the repo root whose `image_path` column points at **`./creatives/<file>.png`**.
   Columns, in order: `image_path,intro_text,headline,call_to_action,destination_url`
3. `git commit` + `git push` → wait for Railway to redeploy (~1–2 min).
4. Call `bulk_create_single_image_ads(account_id, campaign_id, csv_path="<file>.csv", status="DRAFT")`
   once per ad set. The server reads the CSV and images off its own disk.

Same creatives across multiple ad sets = one CSV, run once per `campaign_id`.

## Step-by-step (copy/paste)

```bash
# 0. clone once if you don't have it locally
git clone https://github.com/1stdata/Linkedin-Ads-MCP.git ~/Linkedin-Ads-MCP
cd ~/Linkedin-Ads-MCP

# 1. drop the images in creatives/ with clean names (no spaces)
mkdir -p creatives
cp "/path/to/AD 1a.png" creatives/FIN_1a.png
cp "/path/to/AD 1b.png" creatives/FIN_1b.png
# ...etc

# 2. add the CSV at repo root (image_path = ./creatives/FIN_1a.png ...)
cp "/path/to/bulk_fin.csv" bulk_fin.csv

# 3. deploy
git add creatives/ bulk_fin.csv
git commit -m "add creatives + bulk csv"
git push        # Railway redeploys on push to the deployed branch (main)
```

Then ask Claude to run, per ad set:
`bulk_create_single_image_ads(account_id=507196009, campaign_id=<AD_SET_ID>, csv_path="bulk_fin.csv", status="DRAFT")`

Or use the helper: `./deploy_creatives.sh creatives_source_dir "img1.png" "img2.png" ...` (see `deploy_creatives.sh`).

## CSV rules

- Header row exactly: `image_path,intro_text,headline,call_to_action,destination_url`
- `image_path` is **relative to the repo root**: `./creatives/NAME.png`
- Quote any field containing a comma (intro text usually needs quoting). No em dashes.
- `call_to_action` must be a valid LinkedIn CTA: `LEARN_MORE, SIGN_UP, DOWNLOAD, REGISTER,
  REQUEST_DEMO, SUBSCRIBE, APPLY, JOIN, ATTEND, GET_QUOTE, VIEW_QUOTE, SEE_MORE`.
- `destination_url` can be a placeholder when a Lead Gen form is attached later (the form drives the click).

## Gotchas / pre-flight checklist

- [ ] Image names have **no spaces** (avoids CSV quoting headaches). Rename on copy.
- [ ] CSV `image_path` uses `./creatives/...`, not an absolute or Mac path.
- [ ] Pushed to the **branch Railway deploys** (default `main`).
- [ ] Waited for the redeploy to finish before running the tool (else file-not-found → just retry).
- [ ] `status="DRAFT"` (you canNOT create at PAUSED; ACTIVE sends it straight to review).
- [ ] `owner_org_urn` = the Page URN (Framework Security: `urn:li:organization:40686922`).
- [ ] After creation: attach the **Lead Gen form** in Campaign Manager (not API-automatable),
      then flip DRAFT → ACTIVE to send to review.

## Why not the local CLI?

`bulk_create_ads.py` runs the same pipeline but **on whatever machine you run it on**, so it reads
*local* images — that only helps if the repo is cloned locally AND its `.env` has the LinkedIn token.
The commit-and-deploy method above keeps everything in the MCP and needs no local Python/token setup.

## Verifying it worked

A successful run returns one `urn:li:sponsoredCreative:<id>` per row, e.g. `Bulk create: 4/4 ads created.`
The ads appear as DRAFT in the target ad set. If you see `0/4` or a file-not-found, the deploy hadn't
finished or the `image_path`/branch was wrong — fix and re-run (DRAFT reruns are safe, they just add ads).
