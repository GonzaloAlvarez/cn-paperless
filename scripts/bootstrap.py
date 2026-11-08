#!/usr/bin/env python3
"""cn-paperless/scripts/bootstrap.py — idempotent first-run objects through the
Paperless REST API (called by setup.sh; safe to re-run; stdlib only — kaiser
has no jq).

Logs in as the break-glass superuser (PAPERLESS_ADMIN_USER/PASSWORD from .env,
HTTP Basic on the loopback-published API) and ensures:
  - group `documents`: the household capability group (view/add/change
    documents, tags, correspondents, types, saved views, custom fields,
    notes, share links). Authentik's `documents` group maps onto it at
    login (PAPERLESS_SOCIAL_ACCOUNT_SYNC_GROUPS).
  - user `scanner` (member of `documents`, no superuser) + API token: the
    identity cn-printer's forwarder uploads with (printer.cn_printer_env
    PAPERLESS_TOKEN).
  - user `metrics` (staff, view-only) + API token: paperless-exporter
    (PAPERLESS_METRICS_TOKEN in this stack's .env → exporter/auth-token.txt).
  - tags `scanned` and `gdrive-import`.
  - workflow "Household documents": every consumed document gets view +
    change permission for `documents`, so uploads by `scanner` (its owner)
    are visible to the household.
Tokens are printed ONCE, when the user is created — store them in Kauket
right away; a lost token means deleting the user under /admin/ and re-running.
"""
import base64
import datetime
import json
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request

API = os.environ.get("PAPERLESS_API", "http://localhost:8000/api")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DOC_PERMS = [
    "view_document", "add_document", "change_document", "delete_document",
    "view_tag", "add_tag", "change_tag", "delete_tag",
    "view_correspondent", "add_correspondent", "change_correspondent", "delete_correspondent",
    "view_documenttype", "add_documenttype", "change_documenttype", "delete_documenttype",
    "view_storagepath",
    "view_savedview", "add_savedview", "change_savedview", "delete_savedview",
    "view_customfield", "add_customfield", "change_customfield", "delete_customfield",
    "view_note", "add_note", "change_note", "delete_note",
    "view_uisettings", "change_uisettings",
    "view_paperlesstask",
    "view_sharelink", "add_sharelink", "delete_sharelink",
]
METRICS_PERMS = [
    "view_document", "view_tag", "view_correspondent", "view_documenttype",
    "view_storagepath", "view_paperlesstask", "view_savedview", "view_customfield",
]


def log(msg):
    print(f"\033[1;36m[bootstrap]\033[0m {msg}", flush=True)


def die(msg):
    print(f"\033[1;31m[FAIL]\033[0m {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def env_get(key):
    try:
        with open(os.path.join(ROOT, ".env")) as f:
            for line in f:
                if line.startswith(key + "="):
                    return line.rstrip("\n").split("=", 1)[1]
    except FileNotFoundError:
        die(".env missing")
    return ""


ADMIN_USER = env_get("PAPERLESS_ADMIN_USER")
ADMIN_PASS = env_get("PAPERLESS_ADMIN_PASSWORD")
if not ADMIN_USER or not ADMIN_PASS:
    die("PAPERLESS_ADMIN_USER / PAPERLESS_ADMIN_PASSWORD missing in .env")


def api(method, path, data=None, form=None, auth=True):
    """Returns (status, parsed-json-or-text)."""
    headers = {"Accept": "application/json; version=10"}
    if auth:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{ADMIN_USER}:{ADMIN_PASS}".encode()).decode()
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    elif form is not None:
        body = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(API + path, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except urllib.error.URLError as e:
        die(f"API unreachable at {API}: {e}")


def first(resp):
    return (resp.get("results") or [None])[0] if isinstance(resp, dict) else None


def ensure_group(name, perms):
    code, resp = api("GET", f"/groups/?name__iexact={urllib.parse.quote(name)}")
    existing = first(resp) if code == 200 else None
    payload = {"name": name, "permissions": perms}
    if existing:
        code, resp = api("PATCH", f"/groups/{existing['id']}/", payload)
        if code != 200:
            die(f"PATCH group {name}: {code} {resp}")
        log(f"  ✓ group {name} (id={existing['id']}, permissions synced)")
        return existing["id"]
    code, resp = api("POST", "/groups/", payload)
    if code != 201:
        die(f"POST group {name}: {code} {resp}")
    log(f"  + group {name} (id={resp['id']})")
    return resp["id"]


def ensure_tag(name):
    # Tags are UNOWNED (owner null) so every user — the household and the
    # `scanner` service user — can see and reuse them. An owned tag is invisible
    # to other users, and the forwarder would then create a duplicate.
    code, resp = api("GET", f"/tags/?name__iexact={urllib.parse.quote(name)}")
    existing = first(resp) if code == 200 else None
    if existing:
        if existing.get("owner") is not None:
            code, resp = api("PATCH", f"/tags/{existing['id']}/", {"owner": None})
            if code != 200:
                die(f"PATCH tag {name} owner: {code} {resp}")
            log(f"  ✓ tag {name} (id={existing['id']}, owner cleared)")
        else:
            log(f"  ✓ tag {name} (id={existing['id']})")
        return existing["id"]
    code, resp = api("POST", "/tags/", {"name": name, "matching_algorithm": 0, "is_inbox_tag": False, "owner": None})
    if code != 201:
        die(f"POST tag {name}: {code} {resp}")
    log(f"  + tag {name} (id={resp['id']}, unowned)")
    return resp["id"]


def ensure_user(username, groups, perms, staff, env_key):
    code, resp = api("GET", f"/users/?username__iexact={urllib.parse.quote(username)}")
    existing = first(resp) if code == 200 else None
    base = {"groups": groups, "user_permissions": perms, "is_staff": staff, "is_active": True, "is_superuser": False}
    if existing:
        code, resp = api("PATCH", f"/users/{existing['id']}/", base)
        if code != 200:
            die(f"PATCH user {username}: {code} {resp}")
        log(f"  ✓ user {username} (id={existing['id']}; token unchanged — it lives in Kauket)")
        return
    password = secrets.token_urlsafe(24)
    code, resp = api("POST", "/users/", {"username": username, "password": password, **base})
    if code != 201:
        die(f"POST user {username}: {code} {resp}")
    uid = resp["id"]
    code, tok = api("POST", "/token/", form={"username": username, "password": password}, auth=False)
    if code != 200 or not isinstance(tok, dict) or not tok.get("token"):
        die(f"could not mint a token for {username}: {code} {tok}")
    log(f"  + user {username} (id={uid}) — API token (shown ONCE):")
    print(f"      {env_key}={tok['token']}", flush=True)


def ensure_workflow(name, group_id):
    code, resp = api("GET", "/workflows/?page_size=100")
    if code != 200:
        die(f"GET workflows: {code} {resp}")
    existing = next((w for w in resp.get("results", []) if w.get("name") == name), None)
    # trigger type 1 = Consumption Started; sources 1 consume folder, 2 API,
    # 3 mail, 4 web UI. action type 1 = Assignment.
    payload = {
        "name": name, "order": 0, "enabled": True,
        # a Consumption trigger must carry a filename/path/mail filter: "*" = everything
        "triggers": [{"type": 1, "sources": [1, 2, 3, 4], "filter_filename": "*", "matching_algorithm": 0}],
        "actions": [{"type": 1, "assign_view_groups": [group_id], "assign_change_groups": [group_id]}],
    }
    if existing:
        code, resp = api("PUT", f"/workflows/{existing['id']}/", payload)
        if code != 200:
            die(f"PUT workflow {name}: {code} {resp}")
        log(f"  ✓ workflow {name} (id={existing['id']}, synced)")
    else:
        code, resp = api("POST", "/workflows/", payload)
        if code != 201:
            die(f"POST workflow {name}: {code} {resp}")
        log(f"  + workflow {name} (id={resp['id']})")


def main():
    code, resp = api("GET", "/users/?page_size=1")
    if code != 200:
        die(f"API login as {ADMIN_USER} failed (HTTP {code}) — is the stack up and the admin created? {str(resp)[:200]}")
    log("group")
    gid = ensure_group("documents", DOC_PERMS)
    log("tags")
    ensure_tag("scanned")
    ensure_tag("gdrive-import")
    log("users")
    ensure_user("scanner", [gid], [], False, "PAPERLESS_TOKEN")
    ensure_user("metrics", [], METRICS_PERMS, True, "PAPERLESS_METRICS_TOKEN")
    log("workflow")
    ensure_workflow("Household documents", gid)
    with open(os.path.join(ROOT, ".bootstrap-done"), "w") as f:
        f.write(datetime.datetime.now().isoformat() + "\n")
    log("done. Tokens printed above (if any) go to Kauket: PAPERLESS_TOKEN → printer.cn_printer_env, "
        "PAPERLESS_METRICS_TOKEN → kaiser.cn_paperless_env (then kauket get + ./setup.sh).")


if __name__ == "__main__":
    main()
