# System Components — single "Restart" action (Option B: self-exit + Docker restart policy)

- **Date:** 2026-07-16
- **Status:** Draft — design approved in brainstorming; pending spec review
- **Author:** Claude Code (brainstormed with Mark)
- **Related:** #304 (bug this fixes), #294 (Settings surfaces only part of config), #242 (config-data volume), #37 (dev→Docker rework)

## Problem / motivation

The Settings **System Components** panel and the `/api/system/*` restart machinery look finished, but the restart/stop/start endpoints were written for the old single-host dev model and are a **silent no-op in the containerized deployment** while still returning `200 "restarted successfully"` (see #304). They `pkill -f run_worker.py` and `Popen("./venv/bin/python run_worker.py")` **inside the API container**, but the worker runs in a *separate* container with its own PID namespace, so nothing is killed or started.

The concrete need behind this: some config changed in the app only takes effect after a component restart (notably the worker's own `WorkerConfig` — concurrency/poll/heartbeat — which is read once at startup; model/phase changes already hot-reload per job via `worker.py:738`). Today that restart requires shelling into the host. This spec makes a restart possible from the dashboard, and fixes the false-success + status bugs along the way.

## Goals

- One **Restart Components** action in the System Components panel that actually cycles the running containers (api + worker) in prod.
- Zero new host privileges — no Docker socket mounted into app containers.
- Honest reporting: the endpoint reports what it actually did (requested a restart), never a fabricated success.
- Fix the two cosmetic panel bugs surfaced in #304: `api: running=false` status probe, and the hardcoded `cardigan-api` container badge.

## Non-goals

- **Not** adopting the Docker-socket approach (Option A). Rejected: the socket is effectively root-on-host; Option B is safer for equal effort.
- **Not** adding a transcript-watcher container (see "Current state" — none exists today). The design accommodates a future watcher with no code change, but deploying one is out of scope.
- **Not** the worker `WorkerConfig` hot-reload (#294). Complementary and arguably lighter for the config-reload case; tracked separately. This spec is the general restart capability (also useful for wedged states).
- **Not** per-component buttons. One action, by user decision.

## Current state (verified 2026-07-16; prod `cardigan01` = `4.3.1.dev6`)

- `api/routers/system.py` (mounted at `/api/system`) provides `GET /status` and `POST /{worker,watcher}/{restart,stop,start}`. The restart/stop/start guts (`_kill_process` `system.py:77`, `_start_component` `system.py:86`) assume a single-host process model and no-op in prod. Confirmed live: `POST /api/system/worker/restart` → `200 {"success": true}` while the worker's heartbeat kept ticking.
- Prod/dev compose both run `api` and `worker` as **separate containers** (`docker-compose.prod.yml`; worker CMD `python run_worker.py`, `Dockerfile.worker:38`), each with `restart: unless-stopped`.
- **No transcript-watcher service exists in either `docker-compose.yml` or `docker-compose.prod.yml`.** The "Transcript Watcher" row therefore has no container behind it in the containerized deployment and shows as down/orange. `watch_transcripts.py` is a dev-era folder-watcher (`time.sleep` loop; no DB access; already POSTs `/api/system/watcher/heartbeat` each loop and ignores the response).
- Worker shutdown is already graceful: `run_worker.py` wires `SIGTERM`/`SIGINT` → `worker.stop()` (sets `running=False`); the run loop then drains in-flight jobs (`worker.py` `await asyncio.gather(*active_tasks)`) before exiting.
- Orphan safety net exists: `reset_stale_jobs(threshold_minutes=10)` resets `in_progress` jobs with stale/null heartbeat back to pending, so a force-exit mid-job cannot permanently wedge a job.
- A DB key-value store already exists: `get_config`/`set_config` (`database.py:1302`/`:1322`), already used by the worker to publish `llm_runtime_status`. Reused here — no new table.
- The API image has **no** docker CLI / `docker` SDK; the socket is mounted only into `watchtower`. (Confirms Option B's "no socket" premise.)

## Design

### Mechanism: one timestamp, N consumers

The restart signal is a single value in the existing config KV table:

- Key: `restart_requested_at`, value: an ISO-8601 UTC timestamp.

Each long-lived component records **its own process start time** at boot and, on each loop iteration, reads `restart_requested_at`. If `restart_requested_at > my_start_time`, the component restarts itself. Because a freshly restarted process has a newer start time than any prior request, the same signal never re-fires after a restart — **idempotent by construction, no flag-clearing, no races**. All components rely on `restart: unless-stopped` to come back.

This trivially satisfies "one action for all three": the API writes one timestamp; every alive component polling it restarts; a component that isn't deployed (the watcher, today) simply has no consumer and the signal is a harmless no-op.

### Per-component behavior

**API** (can exit itself). The `POST /api/system/restart` handler:
1. writes `restart_requested_at = now` via `set_config`,
2. returns its HTTP response,
3. **then** schedules its own shutdown *after the response is flushed* — a FastAPI `BackgroundTask` (or `asyncio.create_task` with a short delay) that sends `SIGTERM` to its own PID. uvicorn handles `SIGTERM` as a graceful shutdown; the process exits; Docker restarts it. Respond-before-exit is what prevents the client from getting a dead socket.

**Worker** (has DB access). At the top of its poll loop (near the existing heartbeat publish in `worker.py` `start()`), it reads `restart_requested_at`. If newer than its start time, it calls the existing `worker.stop()` — stop claiming, drain in-flight jobs, exit — and Docker restarts it. The drain is **bounded by a timeout** (default 60s): if in-flight jobs don't finish in time (wedged worker), it force-exits anyway; the orphaned job is reclaimed by `reset_stale_jobs`. Net: graceful by default, self-healing when stuck. The check is cheap and runs every `poll_interval` (~5s).

**Watcher** (no DB access; only present in dev). It already POSTs `/api/system/watcher/heartbeat`. To keep the mechanism uniform, the watcher includes its own boot time in the heartbeat request body (`{"started_at": "<iso>"}`); the endpoint's response gains a `restart: bool` field computed as `restart_requested_at > started_at`. The watcher reads the response (currently discarded in `send_heartbeat()`) and, on true, exits after its current scan. In prod, with no watcher deployed, this path is simply never exercised.

### Endpoint

- **New:** `POST /api/system/restart` → sets `restart_requested_at`, schedules the API self-restart, returns `{ "requested_at": "<iso>", "components": ["api", "worker"], "message": "Restart requested; components will cycle shortly." }`. The `components` list reflects what's observably alive (fresh heartbeat / API itself), so the response is honest about what will actually restart.
- **`GET /api/system/status`:** unchanged contract, but fix the `api` probe (see below) and include each component's start time / heartbeat so the UI can detect the cycle.
- **Old `POST /{worker,watcher}/{restart,stop,start}`:** remove (they are the #304 no-op/false-success bug). Nothing in the UI should call them after this change. (If any external caller is found, return `410 Gone` pointing at `/api/system/restart` — but a repo search should confirm none exist.)

### Status-probe + label fixes (from #304)

- `api: running=false`: the port-8000 `lsof` probe (`_check_port_in_use`, `system.py:60`) fails in-container. Replace with the trivially-true fact that the API is serving the request (report `running=true` unconditionally in the API's own status handler), keeping the port probe only as a dev fallback.
- Per-row container badge: derive the container name per component instead of hardcoding `cardigan-api`. Rows whose component has no fresh heartbeat and no container (the watcher in prod) render as "not deployed" rather than a false "down".

### UI

- Replace the (non-functional) restart affordances with a single **Restart Components** button on the panel.
- Click → confirm dialog: *"This restarts the API and worker. The dashboard will briefly disconnect and reconnect."*
- On confirm: POST `/api/system/restart`, enter a "restarting…" state, and re-poll `GET /api/system/status` with **reconnect tolerance** — expect the API to blink out (network errors for a few seconds) and return; do not surface transient errors as failures. Each status dot goes stale→fresh as its container cycles; clear the "restarting…" state once heartbeats are fresh again.
- Fire-and-forget: the button returns immediately; the user watches the dots cycle rather than the request blocking until everything is back.

### Auth

No new machinery. `POST /api/system/restart` is a mutating endpoint, so it is already covered by the `X-API-Key` middleware whenever `CARDIGAN_API_KEY` is set (it is not in the exempt path list). On the current unauthenticated tailnet-only box it is open, gated by the confirm dialog; it tightens automatically if/when auth is enabled.

### Error handling & observability

- The self-restart path logs a `job/system` event (or structured log) `restart_requested` with the timestamp and observed components, so the cycle is traceable.
- If `set_config` fails, the endpoint returns `500` and does **not** schedule the API self-restart (no partial/asymmetric restart).
- The worker's drain-timeout force-exit logs a warning naming the job(s) abandoned, so the reclaim is not silent.

## Testing

- **Unit — trigger logic:** given a `restart_requested_at` newer/older than a start time, `should_restart()` returns true/false; after a simulated restart (new start time), an old timestamp does not re-fire.
- **Unit — worker drain:** on trigger, `stop()` drains active tasks then exits; when a task exceeds the timeout, the worker force-exits and the job is left for `reset_stale_jobs`.
- **Integration (extend `tests/integration/test_escalation_e2e.py` harness — real `process_job` + real SQLite):** `POST /api/system/restart` writes `restart_requested_at`; a worker loop iteration observes it and initiates stop.
- **Manual on `cardigan01`:** click Restart Components, confirm both containers cycle (heartbeat gap then fresh), the UI reconnects, and a config change made just before the restart is in effect afterward. The self-exit/Docker-restart loop itself can only be validated live (can't unit-test Docker's restart policy).

## Acceptance criteria

- [ ] `POST /api/system/restart` cycles the api and worker containers in prod (verifiable heartbeat gap), and reports honestly (no fabricated "restarted successfully").
- [ ] A freshly restarted component does not immediately restart again (start-time comparison holds).
- [ ] A worker mid-job drains before restarting; a wedged worker force-exits within the timeout and its job is reclaimed.
- [ ] `GET /api/system/status` reports the API as running in-container; per-row container name is correct; the undeployed watcher renders as "not deployed," not "down."
- [ ] The dashboard survives the API restart (disconnect → reconnect) without a hard error state.
- [ ] The old no-op `/{worker,watcher}/{restart,stop,start}` endpoints are removed (or `410`), with no remaining callers.

## Open questions

- Drain timeout default: 60s proposed. Confirm during implementation against the longest realistic single-job phase; make it a constant (or reuse a worker config value) so it's tunable.
