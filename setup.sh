#!/usr/bin/env bash
# cn-paperless — idempotent bring-up on kaiser.lan.
#
# Prerequisites (host): amun core + `amun docker`, the raidnas NFS mount at
# /home/gonzalo/docker/data/nfs with /paperless/media owned 1024:100, and the
# Kauket-managed .env:
#   kauket get kaiser.cn_paperless_env
#   ./setup.sh
# Checks the NFS contract, renders promtail + the exporter token, brings the
# stack up, waits for the API and runs scripts/bootstrap.py (idempotent).
# Re-run after `git pull` or a `kauket get`. It never generates secrets.
set -euo pipefail
cd "$(dirname "$0")"

case "${1:-}" in
  -h|--help) sed -n '/^#!/d; /^[^#]/q; s/^# \{0,1\}//p' "$0"; exit 0 ;;
  "") ;;
  *) echo "unknown argument: $1 (try --help)" >&2; exit 2 ;;
esac

log()  { printf '\n\033[1;36m[setup]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[FAIL]\033[0m %s\n' "$*" >&2; exit 1; }
env_get() { grep -E "^${1}=" .env 2>/dev/null | head -1 | cut -d= -f2- || true; }

[[ -f .env ]] || die ".env not found. Run: kauket get kaiser.cn_paperless_env"
for k in DOCS_AUTHKEY PAPERLESS_DB_PASSWORD PAPERLESS_SECRET_KEY PAPERLESS_ADMIN_PASSWORD ADMIN_EMAIL INFRA_VPS_TAILNET_IP; do
  [[ -n "$(env_get "$k")" ]] || warn "$k is empty in .env"
done
[[ -n "$(env_get OIDC_CLIENT_ID)" ]] || warn "OIDC_CLIENT_ID empty — SSO disabled until cn-authentik/setup-paperless-oidc.sh output is in Kauket"

# ── NFS contract ────────────────────────────────────────────────────────────
NFS=/home/gonzalo/docker/data/nfs
MEDIA="$NFS/paperless/media"
[[ "$(stat -f -c %T "$NFS" 2>/dev/null)" == "nfs" ]] || die "$NFS is not an NFS mount (placeholder dir?) — fix the mount first"
[[ -d "$MEDIA" ]] || die "$MEDIA missing — create it once: sudo install -d -o 1024 -g 100 $NFS/paperless $MEDIA"
[[ "$(stat -c %u:%g "$MEDIA")" == "1024:100" ]] || warn "$MEDIA is owned by $(stat -c %u:%g "$MEDIA"); Paperless writes as 1024:100"

# ── local dirs + rendered files ─────────────────────────────────────────────
mkdir -p consume export backups exporter
INFRA_VPS_TAILNET_IP="$(env_get INFRA_VPS_TAILNET_IP)" envsubst < promtail/promtail.yml.tmpl > promtail/promtail.yml
log "rendered promtail/promtail.yml"

TOKEN_FILE=exporter/auth-token.txt
OLD_TOKEN="$(cat "$TOKEN_FILE" 2>/dev/null || true)"
NEW_TOKEN="$(env_get PAPERLESS_METRICS_TOKEN)"
(umask 077; printf '%s' "$NEW_TOKEN" > "$TOKEN_FILE")
[[ -n "$NEW_TOKEN" ]] || warn "PAPERLESS_METRICS_TOKEN empty — paperless-exporter will 401 until scripts/bootstrap.py's token is in Kauket"

# ── bring the stack up ──────────────────────────────────────────────────────
log "docker compose up -d --remove-orphans"
docker compose up -d --remove-orphans

if [[ "$OLD_TOKEN" != "$NEW_TOKEN" ]]; then
  log "metrics token changed — restarting paperless-exporter"
  docker compose restart paperless-exporter >/dev/null
fi

log "waiting for the Paperless API (up to 5m; first boot runs migrations)"
for i in $(seq 1 60); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -H 'Accept: application/json; version=10' http://localhost:8000/api/ || true)"
  # unauthenticated /api/ answers 302 (→ login) or 401 depending on Accept; both mean "up"
  if [[ "$code" =~ ^(200|302|401)$ ]]; then log "API answers (HTTP $code) after ${i}x5s"; break; fi
  sleep 5
  [[ $i -eq 60 ]] && die "API never answered on localhost:8000 (last HTTP '$code') — docker compose logs webserver"
done

log "bootstrap (group, users, tags, workflow — idempotent)"
command -v python3 >/dev/null || die "python3 is required for scripts/bootstrap.py"
scripts/bootstrap.py

LAB_DOMAIN="$(env_get LAB_DOMAIN)"; LAB_DOMAIN="${LAB_DOMAIN:-lab.gn.al}"
LAN_DOMAIN="$(env_get LAN_DOMAIN)"; LAN_DOMAIN="${LAN_DOMAIN:-kaiser.lan}"
cat <<EOT


===============================================================
cn-paperless is up.

  Paperless      :  https://docs.${LAB_DOMAIN}        (canonical; LAN via step-ca, tailnet via LE)
                    https://docs.${LAN_DOMAIN}        (301 → canonical)
  Login          :  Authentik (groups documents / infra-admins) — local form stays for break-glass
  Media          :  ${MEDIA}  (raidnas NFS, 1024:100)
  Exports        :  ./export (nightly, scripts/export.sh) · pg dumps ./backups · offen Sun 05:30
  Metrics        :  :9100 node · :9187 postgres · :8081 paperless-exporter (VPS scrapes docs.ts.gn.al)

  Then           :  amun docker        # installs the boot unit, reconcile cron and the export cron
===============================================================
EOT
