# gc-host remote access — Cloudflare Tunnel runbook (2026-07-13)

Goal: reach the Grand Central board at **https://gc.example.com**
from any network that allows normal HTTPS (cruise ship Wi-Fi included),
protected by a Cloudflare Access email-OTP login for you@example.com.

Already done (by Claude, 2026-07-13):
- cloudflared installed (brew), logged in to the connectblue.org zone
- Tunnel `grand-central` created (id <tunnel-uuid>)
- DNS: gc.example.com → CNAME to the tunnel
- Ingress config: ~/.cloudflared/config.yml → http://localhost:8080 (validated)
- launchd service written (NOT loaded): config/com.example.grandcentral.tunnel.plist

⚠️ DO STEP 1 BEFORE STEP 2. The moment the tunnel runs, the board is on the
public internet — Access is the only lock on the door.

## Step 1 — Create the Access application (~3 min, in a browser)

1. Go to https://one.dash.cloudflare.com and log in.
   (First visit may ask you to pick a Zero Trust "team name" — anything works,
   e.g. `example`. Free plan is fine, ≤50 users.)
2. Left sidebar: **Access → Applications** → **Add an application**.
3. Choose **Self-hosted**.
4. Fill in:
   - Application name: `gc-host`
   - Session Duration: **1 week** (so the ship doesn't make you re-login daily)
   - Public hostname: subdomain `gc-host`, domain `connectblue.org`
     (leave path blank)
5. Next → **Add a policy**:
   - Policy name: `Bart only`
   - Action: **Allow**
   - Include → selector **Emails** → value `you@example.com`
6. Next through the remaining screens (defaults are fine) → **Add application**.

Login method: the default **One-time PIN** is exactly what we want — no
identity provider setup needed. Cloudflare emails a 6-digit code to
you@example.com at login.

## Step 2 — Start the tunnel service (Terminal on gc-host)

```bash
cp /Users/you/Code/GrandCentral/config/com.example.grandcentral.tunnel.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.example.grandcentral.tunnel.plist
```

Verify it connected (should list 2–4 active connections after ~10s):

```bash
cloudflared tunnel info grand-central
tail -5 /Users/you/Code/GrandCentral/logs/tunnel.err.log
```

## Step 3 — Verify the lock is on

From ANY device/network (phone on cellular is a good test):

1. Open https://gc.example.com
2. You should see a **Cloudflare Access login page** — NOT the board.
   If you see the board directly, STOP the tunnel
   (`launchctl unload ~/Library/LaunchAgents/com.example.grandcentral.tunnel.plist`)
   and re-check Step 1.
3. Enter you@example.com → "Send me a code" → enter the 6-digit code
   from your email → the board loads.

## On the ship

- Just browse to https://gc.example.com — no VPN, no Tailscale.
- Login survives ~1 week per device (per Session Duration). You need to be
  able to read Gmail for the OTP code — do the first login on each device
  BEFORE you leave if possible.
- Everything works as at home: Mail screen, drafts, the confirmed-send dialog.

## Troubleshooting

- **Site unreachable / 1033 error**: tunnel not running on the mini.
  `launchctl kickstart -k gui/$(id -u)/com.example.grandcentral.tunnel`
- **Access page loops / wrong email**: Access → Applications → gc-host →
  edit policy; make sure the email matches exactly.
- **Board 502 through the tunnel**: dashboard itself is down —
  `launchctl kickstart -k gui/$(id -u)/com.example.grandcentral.dashboard`
- **Turn remote access OFF entirely**:
  `launchctl unload ~/Library/LaunchAgents/com.example.grandcentral.tunnel.plist`
  (Tailscale access is unaffected. Plain-LAN access is closed as of E161 —
  see BUILD_SPEC §6; `GC_ALLOW_LAN=1` reopens it.)

Spec note: this amends BUILD_SPEC §6 "local network only" — approved by Bart
2026-07-13, on the condition that Cloudflare Access (email OTP, you@example.com
only) fronts the hostname at all times.
