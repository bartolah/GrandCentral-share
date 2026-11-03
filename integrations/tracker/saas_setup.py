"""Serve t.<domain> for accounts whose DNS isn't on Cloudflare (2026-10-08).

Casey asked whether the tracker only works through Cloudflare; it doesn't have
to. With Cloudflare for SaaS on brightline.example, another domain keeps its DNS
where it is (NameBright, Route 53, …) and adds two records: a CNAME for t.
pointing at trk.brightline.example, and a TXT that lets Cloudflare issue the
certificate. Nothing else on that domain changes — mail records included.

This script does the Cloudflare side, idempotently:
  1. trk.brightline.example — a proxied placeholder record, the fallback origin
  2. sets it as the zone's fallback origin for custom hostnames
  3. a custom hostname t.<domain> per domain, certificate validated by TXT
  4. a Worker route t.<domain>/* → gc-tracker
and prints the exact records each domain's DNS needs.

Needs CLOUDFLARE_API_TOKEN in config/.env (Settings → Connections) with, on
brightline.example: SSL and Certificates (Edit), DNS (Edit), Workers Routes (Edit).

    .venv/bin/python integrations/tracker/saas_setup.py                  # plan + current status
    .venv/bin/python integrations/tracker/saas_setup.py --apply          # do it
    .venv/bin/python integrations/tracker/saas_setup.py --apply consulting.example fieldhouse.example
"""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "sweeper"))

import requests  # noqa: E402
from common import load_env  # noqa: E402

API = "https://api.cloudflare.com/client/v4"
ZONE = "brightline.example"
FALLBACK = "trk.brightline.example"
SCRIPT = "gc-tracker"
DEFAULT_DOMAINS = ["consulting.example", "fieldhouse.example"]


class CF:
    def __init__(self, token: str):
        self.h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def call(self, method: str, path: str, **kw):
        r = requests.request(method, API + path, headers=self.h, timeout=30, **kw)
        d = r.json()
        if not d.get("success"):
            raise RuntimeError(f"{method} {path}: {d.get('errors')}")
        return d.get("result")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("domains", nargs="*", default=DEFAULT_DOMAINS)
    ap.add_argument("--apply", action="store_true", help="make the changes (default: show the plan)")
    a = ap.parse_args(argv)
    token = (load_env().get("CLOUDFLARE_API_TOKEN") or "").strip()
    if not token:
        print("CLOUDFLARE_API_TOKEN isn't set — add it in Settings → Connections.")
        return 1
    cf = CF(token)
    zid = cf.call("GET", f"/zones?name={ZONE}")[0]["id"]

    # 1 + 2: the fallback origin
    recs = cf.call("GET", f"/zones/{zid}/dns_records?name={FALLBACK}")
    if not recs:
        print(f"• {FALLBACK}: proxied placeholder record (AAAA 100::)" + ("" if a.apply else "  [plan]"))
        if a.apply:
            cf.call("POST", f"/zones/{zid}/dns_records",
                    json={"type": "AAAA", "name": FALLBACK, "content": "100::", "proxied": True,
                          "comment": "Grand Central tracker: fallback origin for custom hostnames"})
    fb = None
    try:
        fb = cf.call("GET", f"/zones/{zid}/custom_hostnames/fallback_origin")
    except RuntimeError:
        pass
    if not fb or fb.get("origin") != FALLBACK:
        print(f"• fallback origin → {FALLBACK}" + ("" if a.apply else "  [plan]"))
        if a.apply:
            cf.call("PUT", f"/zones/{zid}/custom_hostnames/fallback_origin", json={"origin": FALLBACK})

    routes = {r["pattern"] for r in cf.call("GET", f"/zones/{zid}/workers/routes")}
    out = []
    for dom in a.domains:
        host = f"t.{dom}"
        have = cf.call("GET", f"/zones/{zid}/custom_hostnames?hostname={host}")
        ch = have[0] if have else None
        if not ch:
            print(f"• custom hostname {host} (certificate by TXT)" + ("" if a.apply else "  [plan]"))
            if a.apply:
                ch = cf.call("POST", f"/zones/{zid}/custom_hostnames",
                             json={"hostname": host, "ssl": {"method": "txt", "type": "dv",
                                                             "settings": {"min_tls_version": "1.2"}}})
        if f"{host}/*" not in routes:
            print(f"• Worker route {host}/* → {SCRIPT}" + ("" if a.apply else "  [plan]"))
            if a.apply:
                cf.call("POST", f"/zones/{zid}/workers/routes", json={"pattern": f"{host}/*", "script": SCRIPT})
        if ch:
            ch = cf.call("GET", f"/zones/{zid}/custom_hostnames/{ch['id']}")
            out.append((dom, host, ch))

    if out:
        print("\nDNS records each domain needs (at its own DNS host):")
    for dom, host, ch in out:
        ssl = ch.get("ssl") or {}
        own = ch.get("ownership_verification") or {}
        print(f"\n== {dom}   (hostname {ch.get('status')}, certificate {ssl.get('status')})")
        print(f"  CNAME  {host:<45} → {FALLBACK}")
        for v in ssl.get("validation_records") or []:
            if v.get("txt_name"):
                print(f"  TXT    {v['txt_name']:<45} = {v['txt_value']}")
        if own.get("name"):
            print(f"  TXT    {own['name']:<45} = {own['value']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
