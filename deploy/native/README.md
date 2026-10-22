# Native deployment (no Docker)

How Cardigan V4 runs on **cardigan02**: an unprivileged Debian 13 LXC with systemd units, a git checkout, and state on a ZFS bind mount. Tracked in #470, and the reasoning is in #442. Running natively removes six of the nine ways the Docker deploy could fail without anyone noticing: there's no registry, no image build, and no seed-once volume.

## Layout

| Path | What |
|---|---|
| `/opt/cardigan` | git checkout, detached at the deployed commit. Owned by `cardigan`. |
| `/opt/cardigan/DEPLOYED_SHA` | last successful deploy: sha, ref, version, time, who, previous sha |
| `/opt/cardigan/LAST_ATTEMPT` | the most recent run that changed anything, `status=ok` or `status=failed` with the reason. A run refused at pre-flight (jobs in progress, dirty tree, bad ref) prints `DEPLOY REFUSED` and leaves it alone |
| `/opt/cardigan-venv` | Python 3.13 venv, installed from `requirements.lock` |
| `/var/lib/cardigan/` | **state**, on the `ocean/cardigan` bind mount: `db/`, `output/`, `transcripts/`, `media/`, `config/llm-config.json`, `knowledge/glossary.md` |
| `/var/www/cardigan` | built dashboard (copied from `web/dist`) |
| `/etc/cardigan/cardigan.env` | non-secret settings (from `cardigan.env.example`) |
| `/etc/cardigan/secrets.env` | secrets, written only by `scripts/sync-native-secrets.sh`; every sync rewrites it, so hand edits are lost. It doesn't restart anything |
| `/var/log/cardigan`, `/var/cache/cardigan` | logs; npm/pip caches |

**Services:**
- `cardigan-api`: uvicorn on `:8100`; runs migrations before it starts
- `cardigan-worker`
- nginx: the dashboard on `:3100`, proxying `/api/` to `127.0.0.1:8100`; plus the **public preview vhost on `:3101`** when `/etc/nginx/cardigan-preview.htpasswd` exists (see below)

**Not deployed here (same as today's prod):** diarization (#473), the HTTP MCP server (#474), API-key auth (#475).

## First install

1. Create the LXC and the `ocean/cardigan` bind mount at `/var/lib/cardigan`. See proxmox-config `containers/cardigan02/`.
2. On the box, as root: `CARDIGAN_DATA_GID=1100 ./install.sh`. This installs packages, creates the user, directories, checkout and venv, and puts the units and nginx site in place. Nothing starts yet.
3. From the Mac: `scripts/sync-native-secrets.sh cardigan02`
4. On the box: `cardigan-deploy <sha>`

## Deploying

```bash
ssh -t cardigan02 tmux new -A -s deploy "'cardigan-deploy $(git rev-parse origin/main); echo \"[exit \$?] press enter to close\"; read'"
```

`cardigan-deploy` only accepts an exact commit or tag; branch names are refused. It refuses to run while jobs are in progress unless you pass `--force`. It stops and reports failure the moment any step fails. The steps:

1. **Check out and install:** checks out the commit, installs from the lock, and reinstalls the package so the version it reports matches the commit.
2. **Dashboard:** builds and publishes it.
3. **Units and nginx:** reinstalls the units and renders the nginx site, including the `:3101` preview vhost if its htpasswd is present.
4. **Drift report:** prints config drift (`scripts/config_drift.py`). Live values are kept.
5. **Restart:** stops the worker, restarts the api (which runs migrations), waits up to 240s for it to report healthy, then starts the worker and checks it hasn't crash-looped after 10s. The worker is stopped first so no job runs against a schema mid-migration.
6. **Verify:** checks that `GET /` reports the expected version and that `:3100` serves the dashboard, then writes `DEPLOYED_SHA`.

**Rollback:** `cardigan-deploy <previous sha>`. The previous sha is the last *successful* deploy: it is printed on failure and stored in `DEPLOYED_SHA`. Commits from before the native deploy (no `requirements.lock` or `deploy/native/`) are refused, since they can't run under this layout. If the api failed to come back, the hint says `cardigan-deploy --force <sha>`, in case the api is up but not answering and the rollback's pre-flight can't read the queue.

**Run it under tmux, as root** (the same ssh login `sync-native-secrets.sh` uses):

```bash
ssh -t cardigan02 tmux new -A -s deploy "'cardigan-deploy <sha>; echo \"[exit \$?] press enter to close\"; read'"
```

Keep the nested quoting exactly as shown: ssh joins its arguments into one string for the remote shell, so the inner single quotes are what keep the whole command inside tmux. The trailing `echo; read` keeps the window open after the script exits, so `DEPLOY FAILED` and the rollback hint stay on screen until you press enter. Reattach with `ssh -t cardigan02 tmux attach -t deploy`.

If a session drops mid-deploy, reattach: the deploy keeps running inside tmux. If it died, the worker may be stopped, so run the deploy again. `tmux new -A` reattaches to an existing `deploy` session instead of starting a new one, so if you land on a finished session, press enter to close it and re-run. `LAST_ATTEMPT` showing `status=started` with no later result means a run died partway. The script warns when it isn't under tmux or screen, and refuses to start while another deploy holds its lock.

`cardigan-deploy` installs itself to `/usr/local/sbin` as part of each run, so a change to the script takes effect on the deploy *after* the one that ships it.

## Config and glossary

- **`llm-config.json`:** `LLM_CONFIG_PATH` points into the state dir. At startup, keys that exist in the repo but are missing from the live file are added; existing values are never overwritten (#440). When the repo *changes* a value, the deploy's drift report shows the difference. Apply it through Settings, or edit the live file.
- **`knowledge/glossary.md`:** the app writes to it, so it lives at `KNOWLEDGE_DIR` in the state dir and was seeded once from the repo. That keeps the git checkout clean. The deploy notes when the repo copy differs.

## Public preview (Cloudflare Tunnel)

`cardigan-preview.riechers.co` is the only public route to Cardigan (#485). It works in three layers:
1. **Cloudflare Access** at the edge.
2. **The tunnel:** the `cloudflared` connector in CT 112 (`preview01`), with ingress in `/etc/cloudflared/config.yml`, forwards to this box's `:3101`.
3. **The origin vhost**, `nginx-cardigan-preview.conf.template`, ported from `Dockerfile.web`. It requires HTTP Basic Auth with a rate limit on password attempts, returns 403 for every config/backend write and `/mcp/`, and hides Settings in the dashboard.

**On/off:**
- The vhost is rendered only while `/etc/nginx/cardigan-preview.htpasswd` is non-empty.
- `scripts/sync-native-secrets.sh` writes that file from `CARDIGAN_WEB_HTPASSWD` (one `user:hash` line per editor). If the secret doesn't resolve, the script **removes** the file, so a missing secret turns the vhost off.
- Either way, the change takes effect at the next `cardigan-deploy`.

**Never** give `:3100` or `:8100` a tunnel ingress. The tunnel targets an **IP**, so moving Cardigan to a new box means editing CT 112's ingress and restarting `cloudflared`.
