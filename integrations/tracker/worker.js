/* Grand Central open/click tracker — E269 (BUILD_SPEC §6, 2026-10-06).
 *
 * Bart: "Let's do it. Yes, this is through our Google Workspace for sales and
 * campaign emails." One Worker serves t.<domain> for every account domain.
 *
 *   GET /o/<token>.gif               log an open, return a 1x1 transparent GIF
 *   GET /c/<token>/<n>/<sig>?u=<url> log a click on link n, 302 to u — ONLY if
 *                                    sig = HMAC-SHA256(SECRET, token|n|u), so it
 *                                    can never be used as an open redirect
 *   GET /events?after=<id>           Grand Central pulls events (Bearer SECRET)
 *   GET /health                      "ok" — GC tracks a domain only once this answers
 *
 * Stored per event: token, kind, link index, time, a coarse source label and
 * the country. Never the recipient's IP address, never any email content.
 * The token is random and means nothing without Grand Central's own table.
 */

const GIF = Uint8Array.from(atob("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"), c => c.charCodeAt(0));
const TOKEN_RE = /^[A-Za-z0-9_-]{16,40}$/;

// Who fetched it. Gmail's proxy fetches when the recipient opens (a real
// open); Apple Mail Privacy Protection and security scanners fetch on
// delivery (automatic). Grand Central also marks anything within 30 seconds
// of sending as automatic.
const SCANNER_ORGS = /(microsoft|proofpoint|mimecast|barracuda|cisco|ironport|forcepoint|trend ?micro|sophos|zscaler|fortinet|symantec|broadcom|amazon|google cloud|digitalocean|ovh|hetzner|linode|akamai)/i;
function source(req) {
  const ua = req.headers.get("user-agent") || "";
  const org = (req.cf && req.cf.asOrganization) || "";
  if (/GoogleImageProxy|ggpht\.com/i.test(ua)) return "proxy-google";
  if (/apple/i.test(org) || (/^Mozilla\/5\.0$/.test(ua.trim()))) return "apple-prefetch";
  if (/YahooMailProxy/i.test(ua)) return "proxy-yahoo";
  if (SCANNER_ORGS.test(org) || /bot|scan|preview|curl|python|wget|headless/i.test(ua)) return "scanner";
  return "client";
}

async function hmac(secret, msg) {
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(msg));
  return btoa(String.fromCharCode(...new Uint8Array(sig))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "").slice(0, 22);
}

async function log(env, req, token, kind, link) {
  await env.DB.prepare(
    "INSERT INTO events (token, kind, link, src, country, host, ts) VALUES (?, ?, ?, ?, ?, ?, ?)"
  ).bind(token, kind, link, source(req), (req.cf && req.cf.country) || null,
         new URL(req.url).hostname, new Date().toISOString().replace(/\.\d+Z$/, "Z")).run();
}

export default {
  async fetch(req, env, ctx) {
    const url = new URL(req.url);
    const parts = url.pathname.split("/").filter(Boolean);

    if (url.pathname === "/health") return new Response("ok", { headers: { "cache-control": "no-store" } });

    if (parts[0] === "o" && parts.length === 2) {
      const token = parts[1].replace(/\.gif$/, "");
      if (TOKEN_RE.test(token)) ctx.waitUntil(log(env, req, token, "open", null));
      return new Response(GIF, { headers: { "content-type": "image/gif",
        "cache-control": "no-store, no-cache, must-revalidate, private", "pragma": "no-cache" } });
    }

    if (parts[0] === "c" && parts.length === 4) {
      const [, token, n, sig] = parts;
      const target = url.searchParams.get("u") || "";
      if (TOKEN_RE.test(token) && /^\d{1,3}$/.test(n) && /^https?:\/\//i.test(target)
          && sig === await hmac(env.SECRET, `${token}|${n}|${target}`)) {
        ctx.waitUntil(log(env, req, token, "click", Number(n)));
        return Response.redirect(target, 302);
      }
      return new Response("Not found", { status: 404 });
    }

    if (url.pathname === "/events") {
      if (req.headers.get("authorization") !== `Bearer ${env.SECRET}`) return new Response("no", { status: 401 });
      const after = Number(url.searchParams.get("after") || 0);
      const { results } = await env.DB.prepare(
        "SELECT id, token, kind, link, src, country, host, ts FROM events WHERE id > ? ORDER BY id LIMIT 500"
      ).bind(after).all();
      return Response.json({ events: results });
    }

    return new Response("Not found", { status: 404 });
  },
};
