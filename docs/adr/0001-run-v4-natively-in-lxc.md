# 1. Run V4 natively in an LXC, not Docker-in-LXC

- **Status:** Accepted, 2026-10-19
- **Deciders:** Mark Riechers, with agent sessions V4-in-LXC and cardigan-4c
- **Tracked in:** #470. Reasoning in #442; parent epic #223.

## Context

Production (`cardigan01`, CT 103) runs Docker-in-LXC: three images from GHCR, pulled by `cardigan-update.timer`. Every step of that chain failed during 2026, and none of the failures made noise (#442). The chain is:

1. CI builds and pushes the images.
2. The registry namespace and permissions have to allow it.
3. The LXC pulls them.
4. Compose recreates the containers.
5. The app reads its config from a seed-once volume.

The failures:
- The namespace moved when the repo changed orgs (#352).
- The org packages aren't linked to the repo, so the push is still denied as of this ADR.
- The config volume never received new settings (#440).
- The LXC's compose file is a hand-edited copy (#180).
- `:latest` tags carry no provenance (#318).

A 401 on pull looks exactly like "nothing new to deploy". Prod has run the 2026-07-20 images since July.

Most of the homelab fleet already runs natively. Debian 13 ships Python 3.13 (what `pyproject.toml` requires) and Node 20 (what `Dockerfile.web` builds with).

## Decision

Run Cardigan V4 **natively** under systemd on a fresh, unprivileged Debian 13 LXC, **cardigan02** (CT 108). Validate it against a copy of prod data, then move the `cardigan01` name onto it. CT 103 stays available for two weeks as rollback.

The deployment is defined in `deploy/native/` (see its README):

| What | How |
|---|---|
| Code | A git checkout, deployed by exact commit with `cardigan-deploy <sha\|tag>`: pushed by hand, never a branch and never a timer |
| Dependencies | `requirements.lock` (#477). The Dockerfiles and CI install from it too. |
| Config | `llm-config.json` lives in the state directory and is seeded additively (#440/#478). The deploy prints value drift. |
| State | A ZFS bind mount, `ocean/cardigan` → `/var/lib/cardigan` (DB, outputs, transcripts, config, glossary) |
| Provenance | `/opt/cardigan/DEPLOYED_SHA`, plus a deploy-time check that `GET /` reports the expected version |
| Scope | Parity with today's prod: api, worker, web. Diarization (#473), HTTP MCP (#474) and API-key auth (#475) are follow-ups. |

## Consequences

**Removes by design** five of #442's nine failure modes, and handles a sixth in code:
- removed: GHCR namespace and visibility (1, 7), the hand-edited compose file (5), mixed commits from `:latest` (6), and a failed pull that looks like a no-op (9)
- handled: the seed-once config (4) is handled by additive seeding, and the deploy reports drift

GitHub Actions is no longer on the deploy path, so a billing lock or a registry permission can't freeze prod.

**Gains:**
- Every deploy step checks its result and fails loudly.
- Rollback is `cardigan-deploy <previous sha>`.
- Prod data gets backups for the first time, through ZFS snapshots.
- `zfs clone` gives V5 and staging boxes cheap copies of prod data, once a test mode exists (#472).

**Costs:**
- The box builds the web dashboard itself, which needs Node and about 1 GB of headroom per deploy.
- System packages (ffmpeg, nginx, Node) now follow Debian's versions rather than an image's.
- With a bind mount in place, `pct snapshot` stops working for the CT. Schema rollback means restoring a ZFS snapshot, because migrations only go forward.
- Rolling back after cutover loses jobs that ran on cardigan02 since then. That's accepted.
- ~~After cutover the box's hostname (`cardigan02`) and its tailnet name (`cardigan01`) differ.~~ Changed at cutover (2026-10-26): no host was renamed. Clients use the service name `cardigan.riechers.co` (Caddy on the LAN, a Cloudflare Access-gated tunnel outside), and hosts keep their own names (#476).
- **Diarization** still needs a container (whisperx/torch) wherever it eventually runs.

**Not decided here:** how V5 deploys. This ADR covers V4 only; V5 planning is on its own map (#423).
