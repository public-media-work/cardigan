#!/usr/bin/env bash
# deploy/native/install.sh — one-time bootstrap of a native Cardigan box (#470).
#
# Run as root on a fresh unprivileged Debian 13 LXC, AFTER the ocean/cardigan
# bind mount is in place at /var/lib/cardigan. Idempotent: re-running only
# fills in what is missing. It starts nothing of Cardigan's and deploys no code — that
# is cardigan-deploy's job, once secrets are synced.
#
# Usage (on the box):
#   curl -fsSL https://raw.githubusercontent.com/public-media-work/cardigan/main/deploy/native/install.sh | bash
#   # or, from an existing checkout:
#   sudo ./deploy/native/install.sh
#
# Env:
#   CARDIGAN_DATA_GID  numeric GID that owns the bind-mounted state (the
#                      proxmox-config idmap pattern uses 1100). If set, the
#                      cardigan user joins that group, and state directories
#                      this script creates get that group, mode 2775.
#                      Default: unset (cardigan:cardigan, 0755).
#   CARDIGAN_REPO      clone URL. Default: the public GitHub repo.
set -euo pipefail

REPO_URL="${CARDIGAN_REPO:-https://github.com/public-media-work/cardigan.git}"
APP=/opt/cardigan
VENV=/opt/cardigan-venv
STATE=/var/lib/cardigan
ETC=/etc/cardigan

[[ $EUID -eq 0 ]] || { echo "install.sh must run as root" >&2; exit 1; }

echo "==> apt packages"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  python3 python3-venv ffmpeg nginx nodejs npm git sqlite3 gettext-base rsync curl ca-certificates tmux >/dev/null
python3 -c 'import sys; assert sys.version_info[:2] >= (3, 13), sys.version' \
  || { echo "Python >= 3.13 required (pyproject.toml); Debian 13 ships 3.13" >&2; exit 1; }

echo "==> cardigan user"
if ! id cardigan >/dev/null 2>&1; then
  useradd --system --home-dir "$APP" --no-create-home --shell /usr/sbin/nologin cardigan
fi
if [[ -n "${CARDIGAN_DATA_GID:-}" ]]; then
  getent group "$CARDIGAN_DATA_GID" >/dev/null || groupadd --gid "$CARDIGAN_DATA_GID" cardigan-data
  usermod -aG "$(getent group "$CARDIGAN_DATA_GID" | cut -d: -f1)" cardigan
fi

echo "==> directories"
install -d -o cardigan -g cardigan "$APP" "$VENV" /var/log/cardigan /var/www/cardigan /var/cache/cardigan
# State lives on the bind mount, whose ownership belongs to the Proxmox side
# (idmap). Create what is missing; never re-chown or re-mode a directory that
# already exists -- in an unprivileged LXC that can fail on unmapped IDs or
# silently strip group write.
if [[ -n "${CARDIGAN_DATA_GID:-}" ]]; then STATE_GROUP="$CARDIGAN_DATA_GID"; STATE_MODE=2775; STATE_FILE_MODE=0664
else STATE_GROUP=cardigan; STATE_MODE=0755; STATE_FILE_MODE=0644; fi
for d in "$STATE" "$STATE"/{db,output,transcripts,media,config,knowledge}; do
  [[ -d "$d" ]] || install -d -o cardigan -g "$STATE_GROUP" -m "$STATE_MODE" "$d"
done
install -d -o root -g cardigan -m 0750 "$ETC"

echo "==> checkout"
if [[ ! -d "$APP/.git" ]]; then
  runuser -u cardigan -- env HOME=/var/cache/cardigan git clone --quiet "$REPO_URL" "$APP"
fi

echo "==> venv"
[[ -x "$VENV/bin/python" ]] || runuser -u cardigan -- env HOME=/var/cache/cardigan python3 -m venv "$VENV"

echo "==> config"
if [[ ! -f "$ETC/cardigan.env" ]]; then
  install -o root -g cardigan -m 0640 "$APP/deploy/native/cardigan.env.example" "$ETC/cardigan.env"
fi
# The glossary is written by the app; seed it once from the repo copy.
if [[ ! -f "$STATE/knowledge/glossary.md" && -f "$APP/knowledge/glossary.md" ]]; then
  install -o cardigan -g "$STATE_GROUP" -m "$STATE_FILE_MODE" "$APP/knowledge/glossary.md" "$STATE/knowledge/glossary.md"
fi

echo "==> deploy command, units, nginx site"
install -m 0755 "$APP/deploy/native/cardigan-deploy" /usr/local/sbin/cardigan-deploy
install -m 0644 "$APP"/deploy/native/cardigan-{api,worker}.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --quiet cardigan-api.service cardigan-worker.service
rm -f /etc/nginx/sites-enabled/default
ln -sf /etc/nginx/sites-available/cardigan /etc/nginx/sites-enabled/cardigan

cat <<EOF

Bootstrap done. Nothing is running yet, and the nginx site doesn't exist until
the first deploy renders it -- don't restart nginx before then. Next:
  1. From the Mac:  scripts/sync-native-secrets.sh <host>
  2. Review $ETC/cardigan.env
  3. On the box:    cardigan-deploy <sha-or-tag>
EOF
