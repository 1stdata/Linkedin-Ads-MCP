#!/usr/bin/env bash
# deploy_creatives.sh — copy image files into the repo's creatives/ folder and push,
# so the Railway-hosted MCP can read them at ./creatives/<name>.png.
#
# Usage (run from anywhere inside the repo):
#   ./deploy_creatives.sh "/path/to/AD 1a.png" "/path/to/AD 1b.png" ...
#
# Spaces in filenames are converted to underscores. After it pushes, wait ~1-2 min
# for Railway to redeploy, then run bulk_create_single_image_ads via the MCP.
set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "Usage: $0 <image1> [image2 ...]" >&2
  exit 1
fi

cd "$(git rev-parse --show-toplevel)"
mkdir -p creatives

added=()
for src in "$@"; do
  if [ ! -f "$src" ]; then echo "skip (not found): $src" >&2; continue; fi
  base="$(basename "$src")"
  clean="${base// /_}"
  cp "$src" "creatives/$clean"
  echo "copied -> creatives/$clean"
  added+=("creatives/$clean")
done

if [ "${#added[@]}" -eq 0 ]; then echo "nothing copied; aborting" >&2; exit 1; fi

git add "${added[@]}"
git commit -m "add creatives: ${added[*]}"
git push
echo
echo "Pushed. Wait ~1-2 min for Railway to redeploy, then run (per ad set):"
echo '  bulk_create_single_image_ads(account_id=..., campaign_id=..., csv_path="your.csv", status="DRAFT")'
