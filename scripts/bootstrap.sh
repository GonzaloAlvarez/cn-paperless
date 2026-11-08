#!/usr/bin/env bash
# cn-paperless/scripts/bootstrap.sh — idempotent first-run objects through the
# Paperless REST API (called by setup.sh; safe to re-run).
#
# Logs in as the break-glass superuser (PAPERLESS_ADMIN_USER/PASSWORD from .env,
# HTTP Basic on the loopback-published API) and ensures:
#   - group `documents`: the household capability group (view/add/change
#     documents, tags, correspondents, types, saved views, custom fields,
#     notes, share links). Authentik's `documents` group maps onto it at
#     login (PAPERLESS_SOCIAL_ACCOUNT_SYNC_GROUPS).
#   - user `scanner` (member of `documents`, no superuser) + API token: the
#     identity cn-printer's forwarder uploads with (printer.cn_printer_env
#     PAPERLESS_TOKEN).
#   - user `metrics` (staff, view-only) + API token: paperless-exporter
#     (PAPERLESS_METRICS_TOKEN in this stack's .env → exporter/auth-token.txt).
#   - tags `scanned` and `gdrive-import`.
#   - workflow "Household documents": every consumed document gets view +
#     change permission for `documents`, so uploads by `scanner` (its owner)
#     are visible to the household.
# Tokens are printed ONCE, when the user is created — store them in Kauket
# right away; a lost token means deleting the user in Django admin and
# re-running this script.
set -euo pipefail
cd "$(dirname "$0")/.."

API="${PAPERLESS_API:-http://localhost:8000/api}"
log() { printf '\033[1;36m[bootstrap]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[FAIL]\033[0m %s\n' "$*" >&2; exit 1; }
env_get() { grep -E "^${1}=" .env 2>/dev/null | head -1 | cut -d= -f2- || true; }

command -v jq >/dev/null || die "jq is required"
[[ -f .env ]] || die ".env missing"
ADMIN_USER="$(env_get PAPERLESS_ADMIN_USER)"; ADMIN_PASS="$(env_get PAPERLESS_ADMIN_PASSWORD)"
[[ -n "$ADMIN_USER" && -n "$ADMIN_PASS" ]] || die "PAPERLESS_ADMIN_USER / PAPERLESS_ADMIN_PASSWORD missing in .env"

api() { # method path [curl args…]
  local method="$1" path="$2"; shift 2
  curl -sS --max-time 60 -u "${ADMIN_USER}:${ADMIN_PASS}" \
       -H "Accept: application/json; version=10" -H "Content-Type: application/json" \
       -X "$method" "${API}${path}" "$@"
}

# ── group: documents ────────────────────────────────────────────────────────
DOC_PERMS='["view_document","add_document","change_document","delete_document",
 "view_tag","add_tag","change_tag","delete_tag",
 "view_correspondent","add_correspondent","change_correspondent","delete_correspondent",
 "view_documenttype","add_documenttype","change_documenttype","delete_documenttype",
 "view_storagepath",
 "view_savedview","add_savedview","change_savedview","delete_savedview",
 "view_customfield","add_customfield","change_customfield","delete_customfield",
 "view_note","add_note","change_note","delete_note",
 "view_uisettings","change_uisettings",
 "view_paperlesstask",
 "view_sharelink","add_sharelink","delete_sharelink"]'

ensure_group() { # name perms_json -> echoes id
  local name="$1" perms="$2" id payload resp
  id=$(api GET "/groups/?name__iexact=${name}" | jq -r '.results[0].id // empty')
  payload=$(jq -nc --arg n "$name" --argjson p "$perms" '{name:$n, permissions:$p}')
  if [[ -n "$id" ]]; then
    resp=$(echo "$payload" | api PATCH "/groups/${id}/" -d @-)
    [[ "$(jq -r '.id // empty' <<<"$resp")" == "$id" ]] || die "PATCH group ${name}: $resp"
    log "  ✓ group ${name} (id=${id}, permissions synced)" >&2
  else
    resp=$(echo "$payload" | api POST "/groups/" -d @-)
    id=$(jq -r '.id // empty' <<<"$resp"); [[ -n "$id" ]] || die "POST group ${name}: $resp"
    log "  + group ${name} (id=${id})" >&2
  fi
  printf '%s\n' "$id"
}

# ── users + tokens ──────────────────────────────────────────────────────────
ensure_user() { # username groups_json perms_json is_staff -> prints token on creation
  local user="$1" groups="$2" perms="$3" staff="$4" id payload resp pass token
  id=$(api GET "/users/?username__iexact=${user}" | jq -r '.results[0].id // empty')
  if [[ -n "$id" ]]; then
    payload=$(jq -nc --argjson g "$groups" --argjson p "$perms" --argjson s "$staff" \
      '{groups:$g, user_permissions:$p, is_staff:$s, is_active:true, is_superuser:false}')
    resp=$(echo "$payload" | api PATCH "/users/${id}/" -d @-)
    [[ "$(jq -r '.id // empty' <<<"$resp")" == "$id" ]] || die "PATCH user ${user}: $resp"
    log "  ✓ user ${user} (id=${id}; token unchanged — stored in Kauket)"
    return 0
  fi
  pass=$(openssl rand -base64 24)
  payload=$(jq -nc --arg u "$user" --arg pw "$pass" --argjson g "$groups" --argjson p "$perms" --argjson s "$staff" \
    '{username:$u, password:$pw, groups:$g, user_permissions:$p, is_staff:$s, is_active:true, is_superuser:false}')
  resp=$(echo "$payload" | api POST "/users/" -d @-)
  id=$(jq -r '.id // empty' <<<"$resp"); [[ -n "$id" ]] || die "POST user ${user}: $resp"
  token=$(curl -sS --max-time 60 -H "Accept: application/json; version=10" \
            -X POST "${API}/token/" --data-urlencode "username=${user}" --data-urlencode "password=${pass}" \
          | jq -r '.token // empty')
  [[ -n "$token" ]] || die "could not mint a token for ${user}"
  log "  + user ${user} (id=${id}) — API token (shown ONCE):"
  printf '      %s=%s\n' "$5" "$token"
}

# ── tags ────────────────────────────────────────────────────────────────────
ensure_tag() { # name -> id
  local name="$1" id resp
  id=$(api GET "/tags/?name__iexact=${name}" | jq -r '.results[0].id // empty')
  if [[ -z "$id" ]]; then
    resp=$(jq -nc --arg n "$name" '{name:$n, matching_algorithm:0, is_inbox_tag:false}' | api POST "/tags/" -d @-)
    id=$(jq -r '.id // empty' <<<"$resp"); [[ -n "$id" ]] || die "POST tag ${name}: $resp"
    log "  + tag ${name} (id=${id})" >&2
  else
    log "  ✓ tag ${name} (id=${id})" >&2
  fi
  printf '%s\n' "$id"
}

# ── workflow: household permissions on every consumed document ──────────────
ensure_workflow() { # name group_id
  local name="$1" gid="$2" id payload resp
  id=$(api GET "/workflows/?page_size=100" | jq -r --arg n "$name" '.results[] | select(.name==$n) | .id' | head -n1)
  # trigger type 1 = Consumption Started; sources 1 consume folder, 2 API,
  # 3 mail, 4 web UI. action type 1 = Assignment.
  payload=$(jq -nc --arg n "$name" --argjson g "$gid" '{
    name: $n, order: 0, enabled: true,
    triggers: [ { type: 1, sources: [1,2,3,4], matching_algorithm: 0 } ],
    actions:  [ { type: 1, assign_view_groups: [$g], assign_change_groups: [$g] } ]
  }')
  if [[ -n "$id" ]]; then
    resp=$(echo "$payload" | api PUT "/workflows/${id}/" -d @-)
    [[ "$(jq -r '.id // empty' <<<"$resp")" == "$id" ]] || die "PUT workflow ${name}: $resp"
    log "  ✓ workflow ${name} (id=${id}, synced)"
  else
    resp=$(echo "$payload" | api POST "/workflows/" -d @-)
    id=$(jq -r '.id // empty' <<<"$resp"); [[ -n "$id" ]] || die "POST workflow ${name}: $resp"
    log "  + workflow ${name} (id=${id})"
  fi
}

# ── run ─────────────────────────────────────────────────────────────────────
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -u "${ADMIN_USER}:${ADMIN_PASS}" \
         -H "Accept: application/json; version=10" "${API}/users/?page_size=1")
[[ "$code" == "200" ]] || die "API login as ${ADMIN_USER} failed (HTTP ${code}) — is the stack up and the admin created?"

log "group"
GID=$(ensure_group documents "$DOC_PERMS")
log "tags"
ensure_tag scanned >/dev/null
ensure_tag gdrive-import >/dev/null
log "users"
ensure_user scanner "[${GID}]" '[]' false PAPERLESS_TOKEN
ensure_user metrics '[]' '["view_document","view_tag","view_correspondent","view_documenttype","view_storagepath","view_paperlesstask","view_savedview","view_customfield"]' true PAPERLESS_METRICS_TOKEN
log "workflow"
ensure_workflow "Household documents" "$GID"
date -Is > .bootstrap-done
log "done. Tokens printed above (if any) go to Kauket: PAPERLESS_TOKEN → printer.cn_printer_env, PAPERLESS_METRICS_TOKEN → kaiser.cn_paperless_env (then ./setup.sh)."
