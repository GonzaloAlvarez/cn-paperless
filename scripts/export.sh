#!/usr/bin/env bash
# cn-paperless/scripts/export.sh — nightly incremental document_exporter run
# into ./export (installed as a cron job by amun-docker from resiliency.yml;
# also fine by hand). The export is Paperless's portable, version-matched
# backup: originals + archive PDFs + thumbnails + a JSON manifest of the
# database. offen ships ./export + ./backups off-site on Sunday 05:30.
#
# -d  deletes files from ./export that no longer exist in Paperless
# -c  compares checksums instead of mtimes (safe across the NFS media tree)
set -euo pipefail
cd "$(dirname "$0")/.."

log() { printf '%s [export] %s\n' "$(date -Is)" "$*"; }

if ! docker compose -p cn-paperless ps --status running --services 2>/dev/null | grep -qx webserver; then
  log "webserver is not running — skipping (the boot unit / reconciler will bring it back)"
  exit 0
fi
mkdir -p export
log "document_exporter ../export --delete --compare-checksums"
docker compose -p cn-paperless exec -T webserver document_exporter ../export --delete --compare-checksums --no-progress-bar
log "done: $(find export -type f | wc -l) files, $(du -sh export | cut -f1)"
