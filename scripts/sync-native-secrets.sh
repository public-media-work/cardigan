#!/usr/bin/env bash
# scripts/sync-native-secrets.sh — push Cardigan secrets to a native box (#470).
#
# Resolves each secret with get-secret.sh (which owns the lookup order) and
# writes /etc/cardigan/secrets.env on the target over ssh, via stdin — values
# never appear in argv, the terminal, or a local file. The units load it with
# EnvironmentFile=; api/services/secrets.py reads them from the environment.
#
# Usage:
#   scripts/sync-native-secrets.sh <ssh-host>            # e.g. cardigan02 (must log in as root)
#   scripts/sync-native-secrets.sh <ssh-host> --restart  # also restart api+worker
#
# CARDIGAN_API_KEY is written empty (auth off, #475) unless get-secret.sh
# resolves it. Any other missing secret aborts the sync (exit 2) — nothing is
# written. The Docker-era equivalent is scripts/sync-homelab-secrets.sh.
set -euo pipefail

REQUIRED=(OPENROUTER_API_KEY AIRTABLE_API_KEY LANGFUSE_PUBLIC_KEY LANGFUSE_SECRET_KEY)
OPTIONAL=(CARDIGAN_API_KEY)

HOST="${1:-}"; RESTART="${2:-}"
[[ -n "$HOST" && "$HOST" != -* ]] || { sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 1; }
command -v get-secret.sh >/dev/null || { echo "get-secret.sh is not on PATH — flag this, don't work around it" >&2; exit 2; }

body="# Written by scripts/sync-native-secrets.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ). Do not edit by hand."$'\n'
add() {
  local key="$1" value="$2"
  [[ "$value" != *"'"* && "$value" != *$'\n'* ]] || { echo "$key contains a quote or newline; refusing" >&2; exit 2; }
  body+="$key='$value'"$'\n'
}

for key in "${REQUIRED[@]}"; do
  value=$(get-secret.sh "$key" 2>/dev/null) || { echo "Missing secret $key (get-secret.sh failed); nothing written" >&2; exit 2; }
  [[ -n "$value" ]] || { echo "Secret $key resolved empty; nothing written" >&2; exit 2; }
  add "$key" "$value"
  echo "  resolved $key"
done
for key in "${OPTIONAL[@]}"; do
  value=$(get-secret.sh "$key" 2>/dev/null || true)
  add "$key" "$value"
  if [[ -n "$value" ]]; then echo "  resolved $key (set)"; else echo "  resolved $key (empty)"; fi
done

printf '%s' "$body" | ssh "$HOST" 'sh -c "umask 027; cat > /etc/cardigan/secrets.env.new \
  && chown root:cardigan /etc/cardigan/secrets.env.new \
  && chmod 0640 /etc/cardigan/secrets.env.new \
  && mv /etc/cardigan/secrets.env.new /etc/cardigan/secrets.env"'
echo "Wrote /etc/cardigan/secrets.env on $HOST"

if [[ "$RESTART" == "--restart" ]]; then
  ssh "$HOST" 'systemctl restart cardigan-api && systemctl restart cardigan-worker'
  echo "Restarted cardigan-api and cardigan-worker on $HOST"
fi
