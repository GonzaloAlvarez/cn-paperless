# cn-paperless

[Paperless-ngx](https://docs.paperless-ngx.com/) — the homelab's document system: scans and paperwork go in, get OCR'd, tagged and searchable; originals stay on the NAS. Lives on `kaiser.lan` in the homelab "sidecar stack" shape (see [cn-fitness](https://github.com/GonzaloAlvarez/cn-fitness) for the pattern), canonical URL **`https://docs.lab.gn.al`** on both the LAN (step-ca) and the tailnet (LE), login through Authentik.

Feeders: the scan stack on `printer.lan` ([cn-printer](https://github.com/GonzaloAlvarez/cn-printer)) uploads every finished scan through the REST API; the Google Drive migration tool in [cn-opencloud](https://github.com/GonzaloAlvarez/cn-opencloud) `tools/drive-migrate` pushes selected Drive documents the same way.

## Stack

```
ts-docs (tailscale, tag:svc, publishes 127.0.0.1:8000)      ── netns ──┐
  ├─ webserver        ghcr.io/paperless-ngx/paperless-ngx:3.4.2  :8000   │ docs.ts.gn.al  → VPS traefik-lab (docs.lab.gn.al, LE)
  ├─ consul-register  advertises docs.lab.gn.al → docs.ts.gn.al:8000     │ 127.0.0.1:8000 → cn-home traefik-lan (docs.lab.gn.al step-ca; docs.kaiser.lan 301)
  ├─ promtail / node-exporter / postgres-exporter / paperless-exporter   │
  └─ ts-docs-watchdog (recreates dependents after a sidecar restart)     ┘
project bridge 172.30.8.0/24 (pinned): db postgres:18 .10 · redis valkey:9 .11 · tika .12 · gotenberg .13
pg-dump (12h → ./backups) · backup offen (Sun 05:30 → S3 paperless/ + raidnas WebDAV /paperless/)
mount-precheck: refuses to start unless /home/gonzalo/docker/data/nfs is a real NFS mount with paperless/media
```

Why it looks like this:

- **Media on the NAS, index local.** `/volume1/data/paperless/media` (raidnas NFS, written as `1024:100` through `USERMAP_UID/GID`) holds originals, archive PDFs and thumbnails in a human-readable tree (`PAPERLESS_FILENAME_FORMAT` = `year/correspondent/title`). The Tantivy index, classifier and Postgres stay on local volumes: Paperless rebuilds the index from the database and files, never the other way round. Boot and the 5-minute reconciler are NFS-gated (`resiliency.yml` + `mount-precheck`) so a placeholder directory can never masquerade as the library.
- **Pinned addresses, not names.** The webserver shares `ts-docs`'s network namespace, where tailscaled owns `resolv.conf`; it reaches Postgres/Valkey/Tika/Gotenberg by IPAM-pinned bridge addresses.
- **Tika + Gotenberg** are in from day one because the Drive migration keeps Google Docs/Sheets/Slides as editable Office originals; Paperless renders the searchable PDF copy itself.
- **One canonical origin.** `PAPERLESS_URL` and the OIDC redirect URI are `https://docs.lab.gn.al`; `docs.kaiser.lan` is a 301 in cn-home (RomM pattern). LAN clients resolve the name to kaiser through Technitium + the pfSense override, tailnet clients through the VPS.
- **Pins.** Paperless is pinned and `monitor-only` for Watchtower (it migrates the schema on boot — read the release notes, then bump the tag). Postgres major, Tika, Gotenberg and the exporter are pinned and excluded. There is no unpin condition: bumps are deliberate.

## Setup

Once, on the operator Mac:

```sh
# 1. Authentik objects (groups documents/infra-admins, providers paperless + paperless-ios); prints client id/secret
ssh neptune.lan 'cd ~/cn-authentik && ./setup-paperless-oidc.sh'
# 2. NAS tree (NFS writer identity)
ssh kaiser.lan 'sudo install -d -o 1024 -g 100 /home/gonzalo/docker/data/nfs/paperless /home/gonzalo/docker/data/nfs/paperless/media'
# 3. .env from .env.example (secrets via openssl rand; DOCS_AUTHKEY minted on the VPS with --user 2 --tags tag:svc --expiration 1h), then
kauket add kaiser.cn_paperless_env ./paperless.env --dest /home/gonzalo/cn-paperless/.env --mode 0600
# 4. DNS both sides: cn-dnsdhcpd/dns/records.tsv (seed-dns.sh on pki) + cn-home LAN_HOSTS (./deploy) — see homelab/CLAUDE.md
```

On kaiser (fresh clone):

```sh
git clone https://github.com/GonzaloAlvarez/cn-paperless ~/cn-paperless && cd ~/cn-paperless
kauket get kaiser.cn_paperless_env
./setup.sh          # NFS checks, render, up, wait, scripts/bootstrap.py (prints the scanner + metrics tokens ONCE)
amun docker         # boot unit (NFS-gated, after cn-books), reconcile cron, nightly export cron
```

Put the two printed tokens where they belong — `PAPERLESS_TOKEN` into `printer.cn_printer_env` (cn-printer's forwarder), `PAPERLESS_METRICS_TOKEN` into `kaiser.cn_paperless_env` — then `kauket get` + `./setup.sh` again (the exporter restarts with the token). Deploy after a change: `git pull && ./setup.sh`; for a single service `docker compose up -d --force-recreate --no-deps <svc>`.

| Access | URL | Notes |
|---|---|---|
| Web UI | **`https://docs.lab.gn.al`** | Authentik login (`documents` or `infra-admins`); local form for the break-glass admin |
| LAN alias | `https://docs.kaiser.lan` | 301 to the canonical name |
| API | `https://docs.lab.gn.al/api/` | `Authorization: Token …`, `Accept: application/json; version=10` |
| iOS | [Swift Paperless](https://swift-paperless.app/) | OIDC login via the `paperless-ios` public client (redirect `x-paperless://oidc-callback`), or an API token |
| Android | Paperless Mobile | username/password or API token (no OIDC in that app) |

## Identity and permissions

- **Groups are capabilities.** Authentik `documents` → Paperless group `documents` (synced at every login by `PAPERLESS_SOCIAL_ACCOUNT_SYNC_GROUPS`); Authentik `infra-admins` → superuser (`PAPERLESS_SOCIAL_ACCOUNT_SYNC_SUPERUSER_GROUP`). New SSO users are auto-created (`PAPERLESS_SOCIAL_AUTO_SIGNUP`); local signups are closed.
- **Workflow "Household documents"** (created by `scripts/bootstrap.py`): every consumed document — whatever the source — gets view + change permission for `documents`. Without it, documents uploaded by `scanner` would be visible only to `scanner` and superusers.
- **Service users**: `scanner` (member of `documents`; the printer forwarder's token) and `metrics` (staff, view-only; the exporter's token). Tokens are printed once at creation; losing one means deleting the user under `/admin/` and re-running `scripts/bootstrap.py`.
- **Break-glass**: the `PAPERLESS_ADMIN_USER` superuser (first-boot only; password in Kauket) through the local login form or `https://docs.lab.gn.al/admin/`. If Authentik is down, nothing else is needed. Its e-mail must stay **distinct from every person's** (`admin@docs.lab.gn.al`): allauth refuses to auto-link an SSO login to a local account that already owns the address and drops the user on a signup page that fails with "An account already exists with this email address" — which is exactly what happened on the first operator login (2026-11-08) because the admin had been created with `ADMIN_EMAIL`.

## Scanning and importing

- cn-printer's `PDF → Paperless` pipeline moves the finished scan into `/srv/scans/inbox`; its `paperless-forwarder` POSTs it to `/api/documents/post_document/` with the tag `scanned`, waits for the task to finish and files the PDF under `processed/`. Paperless OCRs it (`PAPERLESS_OCR_MODE=auto`), the workflow shares it with the household. Failures stay visible in the scanner's inbox and as `scan_forwarder_*` metrics on kaiser Prometheus.
- `./consume` is a local drop folder (consumer with inotify) for one-off files on kaiser; the API is the normal path.
- Duplicates: Paperless 3 **does not reject** duplicates; it flags them in the Duplicates tab (`has_duplicates=true`). The migration tool's ledger is the idempotency guard.

## State, backup, restore

| What | Where | Backup |
|---|---|---|
| Documents (originals, archive, thumbnails) | raidnas `/volume1/data/paperless/media` (NFS, 1024:100) | durable on the NAS; also inside the nightly export |
| Database | `db_data` volume (Postgres 18) | `pg-dump` every 12 h → `./backups` (14 d / 8 w / 6 m) |
| Export (portable, version-matched) | `./export` — `document_exporter -d -c` nightly (`scripts/export.sh`, cron from `resiliency.yml`) | offen Sun 05:30 → `s3://cloudnet-lab-storage/paperless/` + `http://raidnas.lan:5005/cloudlabbackup/paperless/` (56 d) |
| Index, classifier | `paperless_data` volume | not backed up — rebuilt (`document_index reindex`) |
| Retained secrets | `PAPERLESS_SECRET_KEY`, admin password, OIDC client secret, tokens | Kauket `kaiser.cn_paperless_env` |

Prove a backup: `docker exec cn-paperless-backup-1 /usr/bin/backup`, then check the object in S3 and the file on the WebDAV share. **Restore** (same Paperless version as the export): `kauket get` + `./setup.sh` on an empty install, stop the consumer, untar the export into `./export`, then `docker compose exec -T webserver document_importer ../export` — the importer needs a *completely empty* installation and refuses other versions. A DB-only recovery is `pg_restore` of the newest `./backups` dump into `db` (the media tree is untouched). Watch `du -sh export`: past ~20 GB the weekly tar should give way to restic (cn-immich model).

## Observability

VPS plane (the stack has a tailnet identity): Prometheus jobs `paperless-node` (`docs.ts.gn.al:9100`), `paperless-postgres` (`:9187`) and `paperless` (`:8081`, hansmi/prometheus-paperless-exporter: `paperless_status_*` database/redis/celery/index health, `paperless_statistics_*`, `paperless_task_*`), blackbox probe `https://docs.lab.gn.al/api/` (401 = alive), promtail → VPS Loki under `stack=cn-paperless`, alert group `cn-paperless`. LAN plane: kaiser `blackbox-https` probes `https://docs.kaiser.lan` (301). Glance (`home.lab.gn.al`) and dynacat (`home.kaiser.lan`) list it under Apps.

Health checks first: `docker compose ps` · `docker compose logs --tail=50 webserver` · `curl -s -H 'Accept: application/json; version=10' -H 'Authorization: Token …' https://docs.lab.gn.al/api/status/` · `docker compose exec webserver document_sanity_checker`.

## Gotchas

- **Tags must be unowned.** Paperless hides owned objects from other users: a tag created by the admin is invisible to `scanner`, which then creates a same-named duplicate (happened on the first real scan, 2026-11-07). `scripts/bootstrap.py` creates `scanned`/`gdrive-import` with `owner: null` and clears the owner on existing ones; tags created through the UI by a person follow the same rule if the household should see them.
- **Host header**: `PAPERLESS_URL` sets `ALLOWED_HOSTS`; anything that hits the app with another name gets HTTP 400. Local probes use `http://localhost:8000` (always allowed), never `127.0.0.1`.
- **`USERMAP_UID=1024`**: the container `chown`s `data/`, `media/`, `consume/`, `export/` to `1024:100` on start — expected for the NFS tree (Synology ACL owner), and why `./export` and `./consume` on kaiser end up owned by 1024.
- **No Docker DNS in the netns** — pinned addresses only; `extra_hosts` does not apply to `network_mode: service:`.
- **Watchtower** on kaiser is `cn-books-watchtower-1` (host-wide). Paperless is `monitor-only`: Watchtower reports a new image, you bump the tag after the release notes. Minor versions can bring migrations; the pg-dump before the bump is the rollback.
- **v3 semantics**: `PAPERLESS_OCR_MODE=skip/skip_noarchive` are gone (`auto` + `PAPERLESS_ARCHIVE_FILE_GENERATION`); duplicates are allowed by default; `PAPERLESS_DBENGINE=postgresql` is mandatory.
- The nightly export is `--delete --compare-checksums`: a deleted document disappears from `./export` the next night, so the offsite tarball (56 days of weekly copies) is the undo window.
