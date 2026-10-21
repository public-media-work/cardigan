# Cardigan — Post-Launch Test Plan

A repeatable acceptance runbook for the Cardigan deployment now running in an
**LXC container** on the homelab Proxmox host via **`docker-compose.prod.yml`**
(images pulled from `ghcr.io/mriechers/cardigan-*`).

Run the whole thing once right after launch to prove the stack works
end-to-end. Re-run §1–§3 (the **smoke subset**) as a quick canary after any
`watchtower` auto-update or manual `docker compose pull && up -d`.

**Conventions**
- Run host-side commands from the LXC where the stack lives (the compose project dir).
- The API is published on host port **8100** (`api` container → `:8000`), the web dashboard on **3100**, the optional MCP server on **8180**.
- ✅ = pass, ❌ = fail. Record the date and the running image digest at the top of each run: `docker compose -f docker-compose.prod.yml images`.

---

## §1 — Container & health gate  *(smoke)*

```bash
# All long-running services Up; api + diarization show "healthy"
docker compose -f docker-compose.prod.yml ps

# Direct health probe (this is the same URL the container healthcheck hits)
curl -s http://localhost:8100/api/system/health | python3 -m json.tool
```

**Expected:** `api`, `worker`, `web` (and `tunnel` if the `tunnel` profile is up)
are `Up`; `api` is `healthy`. The health JSON returns:

```json
{ "status": "ok",
  "queue": { "pending": <int>, "in_progress": <int> },
  "llm": { "primary_backend": "...", "phase_backends": { ... } },
  "last_run": ... }
```

> ⚠️ Known-noisy fields: `llm.active_backend`, `llm.active_model`, and
> `last_run` can read `null`/stale even on a healthy system (project memory).
> Judge health on `status: "ok"` + the container being `healthy`, **not** on those fields.

| Check | Result |
|-------|--------|
| All expected containers `Up`, `api` healthy | ☐ |
| `/api/system/health` → `status: ok` | ☐ |

---

## §2 — Auth & access gate  *(smoke)*

Two independent gates once access hardening is in place (see
`PRODUCER_ONBOARDING.md` + the cardigan01 secure-access handoff note):
**Cloudflare Access** at the edge (who reaches the box) and the **origin API key**
(`CARDIGAN_API_KEY`) enforced by the app on every `/api/*` call and the WS.

> Status (2026-06-10): the Cloudflare Tunnel + Access front door is being stood
> up, and `CARDIGAN_API_KEY` is to be enabled once the API image carrying the WS
> header-auth fix is deployed. Until then the API is reachable **unauthenticated
> on the LAN/Tailscale** — a finding to close, not a passing state.

**Origin API-key gate (once `CARDIGAN_API_KEY` is set):**
```bash
# No key → 401 (proves the origin gate is live):
curl -s -o /dev/null -w "%{http_code}\n" http://192.168.1.42:8100/api/jobs            # 401
# With key → 200:
curl -s -o /dev/null -w "%{http_code}\n" -H "X-API-Key: $CARDIGAN_API_KEY" \
  http://192.168.1.42:8100/api/jobs                                                   # 200
# Health stays exempt either way:
curl -s -o /dev/null -w "%{http_code}\n" http://192.168.1.42:8100/api/system/health   # 200
```

**Dashboard + live updates:** open the dashboard, confirm it loads **and** that
job status updates stream without a manual refresh (the WebSocket authenticates
via the `X-API-Key` header the nginx WS proxy injects — regression-critical after
enabling the key).

**Cloudflare Access edge gate (once the tunnel is up):** from a browser outside
the homelab, an un-allowlisted email is blocked at the Cloudflare login wall; an
allowlisted email completes the magic-link and reaches the dashboard.

| Check | Result |
|-------|--------|
| No-key `/api/jobs` → 401; with-key → 200; health → 200 | ☐ |
| Dashboard loads **and** live updates stream (WS authenticated) | ☐ |
| Un-allowlisted email blocked at Access; allowlisted reaches dashboard | ☐ |

---

## §3 — Ingest scan smoke  *(smoke)*

This is the "Ready for Work" surface the scanner feeds (and the target of the
QoL cron work — the QoL cron catalog was removed in the roadmap consolidation; see git history).

```bash
# Trigger a manual scan of the mmingest server
curl -s -X POST http://localhost:8100/api/ingest/scan | python3 -m json.tool

# List what's discovered and still un-actioned
curl -s "http://localhost:8100/api/ingest/available?status=new" | python3 -m json.tool

# Confirm the scheduler/last-scan state
curl -s http://localhost:8100/api/ingest/status | python3 -m json.tool
```

**Expected:** the scan returns counts (`new_files_found`, `new_transcripts`,
`new_screengrabs`); `/available` lists transcripts with `status: "new"`;
`last_scan_at` advances to ~now.

| Check | Result |
|-------|--------|
| `POST /api/ingest/scan` succeeds, returns counts | ☐ |
| `/api/ingest/available?status=new` lists files | ☐ |
| `last_scan_at` updated to ~now | ☐ |

---

## §4 — Full-pipeline acceptance

Proves OpenRouter routing + the `worker` container + the DB write path on real input.

```bash
# Pick one file_id from the /available output above, then queue it:
curl -s -X POST http://localhost:8100/api/ingest/transcripts/queue \
  -H "Content-Type: application/json" \
  -d '{"file_ids": [<FILE_ID>]}' | python3 -m json.tool
```

Watch the job to completion in the dashboard (or poll `GET /api/jobs`). It runs
all four LLM phases.

**Expected:** job reaches `completed`; output contains Release Title, Short/Long
Description, and Keywords that are coherent for the episode.

| Check | Result |
|-------|--------|
| Job queued and picked up by the worker | ☐ |
| All 4 phases complete; status `completed` | ☐ |
| SEO metadata output is coherent | ☐ |

---

## §5 — AirTable write path

The **only** sanctioned write route is `propose → review → confirm`, restricted
to allowlisted fields (Release Title, Short/Long Description, Keywords, social).
Exercise it against a throwaway / test SST record via the MCP server (Claude Desktop
or the `mcp` compose service on :8180).

1. `propose_sst_edit` — stage an edit on the test record.
2. `review_proposed_edits` — preview old → new values.
3. Confirm, then `commit_sst_edits` — writes, and posts an audit comment on the record.

**Expected:** only allowlisted fields change; an audit comment with old/new
values appears on the Airtable record; optimistic-concurrency refuses if the
record changed underneath the proposal.

| Check | Result |
|-------|--------|
| Propose → review shows correct old/new diff | ☐ |
| Commit writes only allowlisted fields | ☐ |
| Audit comment posted on the record | ☐ |

---

## §6 — mmingest search smoke

Reuses the Sprint 5 smoke script. Needs a consumer key with `mmingest:read`.

```bash
# One-time: mint a key (prints plaintext once — copy it)
python3 scripts/create_consumer_key.py --label postlaunch-smoke --scopes mmingest:read

# Run the three canned queries + legacy-endpoint regression pings
CONSUMER_KEY=<key> CARDIGAN_URL=http://localhost:8100 bash scripts/sprint5_smoke_search.sh
cat /tmp/sprint5-smoke-log.jsonl   # inspect results
```

**Expected:** each query returns hits; legacy `/api/jobs` + `/api/system/health`
still 200. Revoke the throwaway key afterwards:
`python3 scripts/create_consumer_key.py --revoke <id>`.

| Check | Result |
|-------|--------|
| Search queries return results | ☐ |
| Legacy endpoints still 200 (no regression) | ☐ |

---

## §7 — Soak / acceptance envelope  *(optional, long-running)*

For a deeper go/no-go, run the 24h soak harness (full runbook in
the sprint-5 staging-soak runbook, removed in the roadmap consolidation; see git history):

```bash
CONSUMER_KEY=<key> bash scripts/sprint5_soak_monitor.sh    # 24h background monitor
python3 scripts/sprint5_soak_report.py                     # GO/NO-GO report
```

**Acceptance envelope:** avg ≤ 1 req/s · peak inflight ≤ 4 · error rate < 1% ·
crawl/DB parity delta = 0 every 6h · p95 search latency < 50 ms.

| Check | Result |
|-------|--------|
| 24h soak completes within envelope | ☐ |
| Report verdict = GO | ☐ |

---

## §8 — Backup / restore drill

The data of record (`dashboard.db`) now lives only on the server — prove it's recoverable.

```bash
# Snapshot the live DB (online-backup API; safe while writing). Produces a
# gzipped copy under .snapshots/.
CARDIGAN_API_CONTAINER=cardigan-v4-api-1 scripts/snapshot_db.sh

ls -lh .snapshots/                 # confirm a fresh dashboard-<ts>.db.gz exists
```

**Restore drill (do this against a scratch copy, not the live volume):**

```bash
gunzip -k .snapshots/dashboard-<ts>.db.gz
# Inspect the restored copy — confirm row counts look sane:
python3 -c "import sqlite3; c=sqlite3.connect('.snapshots/dashboard-<ts>.db'); \
print('jobs:', c.execute('select count(*) from jobs').fetchone()[0])"
```

> Restore-to-live procedure: `docker compose stop api worker`, replace the file
> inside the `db-data` volume with the unzipped snapshot, `up -d`. Document the
> exact volume path the first time you do it.

| Check | Result |
|-------|--------|
| Snapshot lands in `.snapshots/` | ☐ |
| Restored copy opens and row counts are sane | ☐ |

---

## §9 — Restart resilience

Proves the stack and the in-process scheduler come back cleanly.

```bash
docker compose -f docker-compose.prod.yml restart api
sleep 15
curl -s http://localhost:8100/api/system/health | python3 -c "import sys,json;print(json.load(sys.stdin)['status'])"
docker compose -f docker-compose.prod.yml logs api | grep -i "scheduler started"
```

**Expected:** health returns `ok` within the `start_period`; the API log shows
the ingest scheduler re-registering on startup (`api/main.py` lifespan →
`start_scheduler()`).

| Check | Result |
|-------|--------|
| Health returns `ok` after restart | ☐ |
| "Ingest scheduler started" in logs | ☐ |

---

## Sign-off

| Run date | Image digest | Smoke (§1–3) | Full (§4–6) | Soak (§7) | Backup (§8) | Restart (§9) | Notes |
|----------|--------------|--------------|-------------|-----------|-------------|--------------|-------|
|          |              |              |             |           |             |              |       |
