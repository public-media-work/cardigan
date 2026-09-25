# Homelab deploy

How `main` reaches the homelab LXC (`cardigan01`, `cardigan.riechers.co`).

`deploy.yml` builds and pushes
`ghcr.io/public-media-work/cardigan-{api,worker,web}:latest` on every push to
`main`. That only gets images to the registry — something still has to pull them
onto the LXC and restart the stack.

| | Mechanism | Trigger | Latency | Status |
|---|---|---|---|---|
| **A** | `deploy-homelab` job in `deploy.yml` | push to `main` | immediate | **never enabled** |
| **B** | `cardigan-update.timer` (systemd, on the LXC) | every 30 min | ≤30 min | **this is the live path** |

Watchtower used to be the pull+restart half; it was removed for crash-looping on
the modern Docker Engine API.

> **A has never run.** It is gated on `vars.HOMELAB_DEPLOY_ENABLED == 'true'`,
> which has never been set (`gh variable list` returns nothing), so the job is
> skipped on every push. B is what actually deploys. Treat section A below as
> setup instructions for a mechanism that is not in use — not as a description
> of how deploys happen today.

### Production was frozen from 2026-07-20 to 2026-09-25

Worth knowing before trusting anything here. `deploy.yml` authenticated with
`secrets.GITHUB_TOKEN` (scoped to **public-media-work**) while pushing to
**`ghcr.io/mriechers/*`**, a personal namespace it cannot write:

```
denied: permission_denied: The requested installation does not exist
```

Every build failed on push from 2026-07-20 onward, so `:latest` never changed and
B's pulls were all no-ops — the timer was healthy the whole time and had nothing
to fetch. Prod sat on `5e926b8`, 27 commits behind, for two months. The Actions
billing lock that began around 2026-09-12 is a *later, separate* fault; fixing it
alone would not have restored deploys. See #352.

The fix repointed `IMAGE_PREFIX` to the org namespace so the built-in token works.
Two things must hold for that to keep working:

- **Package grant.** `public-media-work/cardigan` needs role **Write** on each org
  package (*Package settings → Manage Actions access*). Packages created by a PAT
  do **not** grant `GITHUB_TOKEN` anything.
- **LXC pull access.** Org packages are private on first push, and the LXC's
  `docker login` was scoped to the old namespace, so the first pull after the flip
  would fail without one of the following.

  **The four org packages are public** (decided 2026-09-25). That is safe here:
  the repo is already public, and the Dockerfiles bake no secrets — the API key
  and the htpasswd line arrive at runtime, interpolated by the web entrypoint and
  mounted as a compose secret respectively. Nothing in an image is not already in
  the repo.

  The payoff is that **the LXC needs no registry credential at all**. Once the
  flip is verified, the stored ghcr login on CT 103 can be removed:

  ```bash
  pct exec 103 -- docker logout ghcr.io
  ```

  To check what is stored without printing the credential:

  ```bash
  pct exec 103 -- python3 -c 'import json;print(list(json.load(open("/root/.docker/config.json")).get("auths",{})))'
  ```

  (`cat` on that file dumps the base64 auth string into your scrollback.)

  *Fallback, if the packages are ever made private again:* refresh the LXC login
  with a token carrying `read:packages` on the org. That puts a credential back on
  the box and gives it an expiry to track, so prefer public.

`cardigan-diarization` is referenced in compose but **not built by `deploy.yml`**,
so it does not follow a prefix change. Copy it registry-side before the flip:

```bash
docker buildx imagetools create \
  -t ghcr.io/public-media-work/cardigan-diarization:latest \
     ghcr.io/mriechers/cardigan-diarization:latest
```

> `docker compose up -d` only recreates a service when its image **digest**
> changed, so a no-op pull never bounces the stack. Running A and B together is
> safe — B just no-ops whenever A already deployed.

---

## A — push-based deploy (one-time setup)

The job is **dormant** until `vars.HOMELAB_DEPLOY_ENABLED == 'true'`. Configure
everything below, then flip that flag last.

### 1. Generate a dedicated CI deploy key

```bash
ssh-keygen -t ed25519 -f ./cardigan_ci_deploy -N "" -C "cardigan-ci-deploy"
```

### 2. Install the public key on the LXC, locked to a forced command

The forced command means this key can run **only** the deploy script — it can't
get a shell, forward ports, or run arbitrary commands.

```bash
FORCED='command="cd /root/cardigan && docker compose pull && docker compose up -d --remove-orphans && docker image prune -f",no-agent-forwarding,no-port-forwarding,no-pty,no-user-rc,no-X11-forwarding'
echo "$FORCED $(cat cardigan_ci_deploy.pub)" | ssh root@192.168.1.42 'cat >> /root/.ssh/authorized_keys'
```

### 3. Create a Tailscale OAuth client (so the CI runner can join the tailnet)

GitHub-hosted runners aren't on your LAN, so the job joins your tailnet as an
ephemeral node and reaches the LXC over Tailscale.

1. Tailscale admin → **Settings → OAuth clients → Generate** with the
   `auth_keys` write scope and tag `tag:ci`.
2. In your tailnet **ACL**, make sure `tag:ci` exists and may SSH to the LXC:
   ```jsonc
   "tagOwners": { "tag:ci": ["autogroup:admin"] },
   "acls": [
     { "action": "accept", "src": ["tag:ci"], "dst": ["<lxc-node>:22"] }
   ]
   ```
   (Regular `sshd` + key auth is used, not Tailscale SSH — only network reach to
   `:22` is needed.)

### 4. Add GitHub repo **secrets** (Settings → Secrets and variables → Actions)

| Secret | Value |
|---|---|
| `TS_OAUTH_CLIENT_ID` | from step 3 |
| `TS_OAUTH_SECRET` | from step 3 |
| `HOMELAB_SSH_KEY` | contents of `cardigan_ci_deploy` (the **private** key) |

```bash
gh secret set HOMELAB_SSH_KEY --repo public-media-work/cardigan < cardigan_ci_deploy
gh secret set TS_OAUTH_CLIENT_ID --repo public-media-work/cardigan
gh secret set TS_OAUTH_SECRET   --repo public-media-work/cardigan
```

### 5. Add GitHub repo **variables**

| Variable | Value |
|---|---|
| `HOMELAB_DEPLOY_HOST` | LXC Tailscale IP (`100.119.247.73`) or MagicDNS name (`cardigan01`) |
| `HOMELAB_DEPLOY_ENABLED` | `true` ← set this **last** to activate |

```bash
gh variable set HOMELAB_DEPLOY_HOST    --repo public-media-work/cardigan --body "100.119.247.73"
gh variable set HOMELAB_DEPLOY_ENABLED --repo public-media-work/cardigan --body "true"
```

Delete the local `cardigan_ci_deploy*` files once the secret is set. Next push to
`main` will deploy automatically; watch the `deploy-homelab` job in Actions.

---

## B — polling fallback (installed, and the live deploy path)

A systemd timer on the LXC pulls + restarts every 30 minutes. Verified healthy on
the LXC 2026-09-04 (#352). Units:
`/etc/systemd/system/cardigan-update.{service,timer}`.

```bash
ssh root@192.168.1.42 'systemctl list-timers cardigan-update.timer'   # next run
ssh root@192.168.1.42 'systemctl start cardigan-update.service'        # force now
ssh root@192.168.1.42 'journalctl -u cardigan-update.service -n 20'    # last run log
```

Change the cadence by editing `OnUnitActiveSec=` in the `.timer` unit, then
`systemctl daemon-reload`. Since A has never been enabled, do **not** lengthen or
disable this — it is the only thing deploying today.

Preferred over enabling A, on balance: B needs no GitHub credentials on the
homelab and no inbound SSH path, and it keeps deploying an already-built image
when Actions is down — which, given the two outages above, is not hypothetical.

### One-off local builds

If images are ever built locally to bridge a CI outage, note that a local
`--platform linux/amd64` build publishes a **single-arch** manifest, where CI
publishes `linux/amd64,linux/arm64`. Nothing in the homelab is arm64 (the host is
an i3-13100), so this is harmless in practice — but anything that later pulls
these tags on arm64 fails with no matching manifest, until CI's next green run
republishes multi-arch.

---

## Manual deploy (anytime)

```bash
ssh root@192.168.1.42 'cd /root/cardigan && docker compose pull && docker compose up -d'
```
