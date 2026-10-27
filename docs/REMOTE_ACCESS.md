# Remote Access — Temporary Preview via Tailscale Funnel

> **Superseded (2026-10-26).** The Funnel was replaced by a Cloudflare Tunnel in
> July, and at the cardigan02 cutover the htpasswd preview vhost gave way to the
> full dashboard at `https://cardigan.riechers.co`, behind a Cloudflare Access
> email allowlist. Add or remove people in the Zero Trust dashboard. The tunnel
> runs in CT 112 (`preview01`); its ingress is `/etc/cloudflared/config.yml`, and
> each origin also needs an egress allow in `/etc/pve/firewall/112.fw`. The rest
> of this page is kept as history.

How to give one or two trusted PBS Wisconsin editors a password-protected door
into *The Metadata Neighborhood* running on the homelab box (`cardigan01`,
CTID 103) — with **nothing for them to install**.

> **This is temporary.** Cardigan's real home is a container at WPM, where access
> will be handled by WPM's internal VPN and none of this app-level gating is
> needed. So this is built to be **cheap to stand up and cheap to tear down** —
> two ingredients that already run on the box (Tailscale + nginx), no new daemon.
> When the WPM VPN lands, one Funnel command and one deleted secret return the box
> to exactly today's state.

The old Cloudflare Tunnel + Access story (PR #208, never executed) is retired.
This replaces it.

---

## How it works

Tailscale **Funnel** exposes one port to the public internet over HTTPS with a
valid cert. Funnel visitors need **no Tailscale account and no client** — that's
what makes it different from ordinary Tailscale node-sharing. Funnel has no login
of its own, so nginx inside the `web` container is the gate.

The Funnel points at a **dedicated hardened vhost** that is separate from the
everyday LAN vhost, so your normal LAN/Tailscale workflow is untouched — no
password prompts locally, Settings still works.

```
Editor's browser
  |  https://cardigan01.<tailnet>.ts.net   (public, valid TLS)
  v
Tailscale Funnel ingress   (TLS terminated at the Tailscale edge; sets X-Forwarded-For)
  |
  v
cardigan01 host :3101  ->  web container nginx :3001   [ HARDENED PREVIEW VHOST ]
  |   - HTTP Basic Auth, realm "Cardigan Preview" (per-person htpasswd)  <- the gate
  |   - rate-limited auth (keyed on real client IP via X-Forwarded-For)
  |   - config WRITES -> 403      (no backend registration / phase-model routing)
  |   - /mcp/          -> 403      (SST-write tools never exposed)
  |   - injects window.__CARDIGAN_PREVIEW__=true into index.html (frontend hides Settings)
  |
  +-- /            SPA (static files)
  +-- /api/        proxy -> api:8000  (producer workflow + config *reads* + estimate-cost)
  +-- /api/ws/     proxy -> api:8000  (live updates)

cardigan01 host :3100  ->  web container nginx :3000   [ EXISTING LAN VHOST — unchanged ]
  (no auth, full surface, LAN / Tailscale only, NOT funneled)
```

Only host port **3101** is funneled. The LAN vhost (`:3100`), the API (`:8100`),
and MCP (`:8180`) are never funneled. The SPA calls `/api/*` **same-origin**
through nginx, so there is no CORS surface to configure.

### The gate is dormant until you switch it on

The hardened vhost, the basic-auth prompt, and the write-blocks are all baked
into the `web` image but **switch on solely by the presence of the secret file
`secrets/cardigan_web_htpasswd`** — the same "empty = off" pattern as
`CARDIGAN_API_KEY`. No secret file → the image behaves **byte-identical to today**
(LAN vhost only, no preview port serving auth).

> **The URL is public — the password is the whole wall.** Tailscale provisions a
> Let's Encrypt cert for `cardigan01.<tailnet>.ts.net`, which publishes that
> hostname to public Certificate Transparency logs. Treat the URL as
> discoverable, not secret. **Use a long, strong, unique password per person**
> (nginx rate-limits the auth, but there's no lockout — a discoverable URL invites
> 24/7 brute force). Never reuse a real account password.

---

## Box-side runbook

All of this runs **on the box host** (`cardigan01`), except the tailnet-admin
step which is in the Tailscale admin console. The Funnel command lives here, not
in `scripts/start.sh`, because it runs on the host outside Docker.

### 1. One-time tailnet admin

In the Tailscale admin console, enable the pieces Funnel needs:

- Turn on the **Funnel** node attribute for `cardigan01`.
- Ensure **HTTPS / MagicDNS** is enabled in the tailnet ACL policy (Funnel needs
  the Let's Encrypt cert path).

### 2. Set a password per editor

Run the helper once per person. It appends a bcrypt `user:hash` line to the
secret file and reminds you to redeploy:

```bash
scripts/set_preview_password.sh          # prompts for username + password
```

Land `secrets/cardigan_web_htpasswd` on the box, then bring the web service up so
nginx picks up the secret and starts serving the hardened vhost:

```bash
docker compose up -d web
```

### 3. Turn on the Funnel

Point Funnel at the hardened host port and run it in the background:

```bash
tailscale funnel --bg 3101
```

Tailscale prints the public URL — `https://cardigan01.<tailnet>.ts.net`.

### 4. Onboard an editor

Send the editor **the URL + their password** over a secure channel — Signal, or a
1Password shared item / link. Nothing for them to install; the browser is the whole
client. Set a teardown / rotation date at the same time (this is a preview, not a
standing service).

### 5. Revoke one person

Delete just their line and redeploy — everyone else keeps working:

```bash
scripts/set_preview_password.sh --revoke <user>
docker compose up -d web
```

### 6. Tear down (when the WPM VPN lands)

```bash
tailscale funnel reset          # stop exposing the port
rm secrets/cardigan_web_htpasswd
docker compose up -d web        # web image goes dormant again
```

The box is now byte-identical to today: LAN vhost only, no auth, no public port.

---

## Verifying it works

Against the **preview port** with the secret present:

- No credentials → **401** (browser shows the "Cardigan Preview" login).
- Valid `user:password` → **200**, dashboard loads.
- A job's phases update live through the Funnel — confirm the live-updates
  **WebSocket** (`/api/ws/jobs`) connects (the browser must pass the cached
  basic-auth credentials on the WS handshake).
- Config **writes** and `/mcp/…` → **403**; config **reads** still **200** (so Job
  Detail and upload cost estimates keep working).
- `$remote_user` shows up in the nginx access log (per-person attribution).
- Repeated bad passwords get rate-limited (429/503 after the burst).

Against the **LAN port** (`:3100`): unchanged — no auth, full Settings, everything
reachable. With the secret **absent**: the preview vhost isn't served at all.

---

## Troubleshooting

| Symptom | Check |
|---------|-------|
| Funnel URL won't resolve / no cert | Funnel node attribute + HTTPS/MagicDNS enabled in the tailnet ACL (step 1); `tailscale funnel status` |
| 401 even with the right password | Secret landed on the box and `docker compose up -d web` was run after editing it |
| Password prompt on the LAN too | You're hitting the preview port (`:3100` is the unchanged, no-auth LAN vhost — use that locally) |
| Live updates never arrive | The WebSocket handshake must carry basic-auth creds; re-open the tab so the browser re-sends them |
| Settings link missing / "unavailable" note | Expected on the preview vhost — Settings is hidden remotely by design (change config over Tailscale on `:8100`) |
| Editor can't get in | Confirm their line is in the htpasswd file and the Funnel is running (`tailscale funnel status`) |
