#!/bin/zsh
# E269 — deploy the open/click tracker Worker (BUILD_SPEC §6, 2026-10-06).
# Needs `wrangler login` (or CLOUDFLARE_API_TOKEN) first. Idempotent: creates
# the D1 database once, applies the schema, sets the Worker's SECRET from
# TRACK_SECRET in config/.env (never printed), deploys, then asks Grand
# Central to re-check t.<domain>/health. Run from anywhere:
#   integrations/tracker/deploy.sh
set -euo pipefail
here="${0:A:h}"; root="${here:h:h}"
cd "$here"
wrangler whoami >/dev/null 2>&1 || { echo "Run 'wrangler login' first."; exit 1; }
secret=$(grep -E '^TRACK_SECRET=' "$root/config/.env" | head -1 | cut -d= -f2-)
[[ -n "$secret" ]] || { echo "TRACK_SECRET is missing — save it in Settings → Connections → Tracking secret."; exit 1; }
if grep -q REPLACE_AFTER_d1_create wrangler.toml; then
  id=$(wrangler d1 create gc-tracker 2>/dev/null | grep -oE '[0-9a-f]{8}-[0-9a-f-]{27}' | head -1 || true)
  [[ -n "$id" ]] || id=$(wrangler d1 list --json | python3 -c "import json,sys; print(next(d['uuid'] for d in json.load(sys.stdin) if d['name']=='gc-tracker'))")
  sed -i '' "s/REPLACE_AFTER_d1_create/$id/" wrangler.toml
fi
wrangler d1 execute gc-tracker --remote --file schema.sql >/dev/null
print -rn -- "$secret" | wrangler secret put SECRET >/dev/null
wrangler deploy
sleep 20   # custom domains take a moment to answer
"$root/.venv/bin/python" "$root/sweeper/tracking.py" --check
