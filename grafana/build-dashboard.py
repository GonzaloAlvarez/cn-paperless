#!/usr/bin/env python3
"""Generate the Paperless-ngx dashboard for the VPS Grafana (grafana.lab.gn.al).

Output (committed into cn-root-docker's provisioning dir):
  <cn-root-docker>/tailnet/grafana/provisioning/dashboards/paperless.json   uid: paperless

Metric sources (all VPS-plane scrapes of docs.ts.gn.al): hansmi/prometheus-paperless-exporter
(job=paperless: paperless_status_*, paperless_statistics_*, paperless_tag_*, paperless_storage_path_*,
paperless_task_*), postgres-exporter (job=paperless-postgres), node-exporter (job=paperless-node),
blackbox-tailnet-http (https://docs.lab.gn.al/api/), promtail → Loki (stack=cn-paperless).
Re-run after editing; keep the uid stable; recreate the VPS grafana container to load it.

    ./build-dashboard.py [path/to/cn-root-docker]   (default ../../cn-root-docker)
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
PROM = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}
PROBE = 'instance="https://docs.lab.gn.al/api/"'
OKFAIL = [{"type": "value", "options": {"0": {"text": "FAIL", "color": "red"}, "1": {"text": "OK", "color": "green"}}}]
UPDOWN = [{"type": "value", "options": {"0": {"text": "DOWN", "color": "red"}, "1": {"text": "UP", "color": "green"}}}]
GREEN_ON_1 = [{"color": "red", "value": None}, {"color": "green", "value": 1}]
RED_ON_1 = [{"color": "green", "value": None}, {"color": "red", "value": 1}]


def q(expr, legend=None, ref="A", instant=False, fmt=None):
    t = {"datasource": PROM, "expr": expr, "refId": ref}
    if legend:
        t["legendFormat"] = legend
    if instant:
        t["instant"] = True
    if fmt:
        t["format"] = fmt
    return t


def row(pid, title, y):
    return {"id": pid, "type": "row", "title": title, "collapsed": False,
            "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": []}


def stat(pid, title, targets, x, y, w, h, unit=None, thresholds=None, decimals=None, mappings=None, color_mode="value", graph="none"):
    fc = {"thresholds": {"mode": "absolute", "steps": thresholds or [{"color": "green", "value": None}]}}
    if unit:
        fc["unit"] = unit
    if decimals is not None:
        fc["decimals"] = decimals
    if mappings:
        fc["mappings"] = mappings
    return {"id": pid, "type": "stat", "title": title, "datasource": PROM, "targets": targets,
            "gridPos": {"x": x, "y": y, "w": w, "h": h}, "fieldConfig": {"defaults": fc, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "colorMode": color_mode, "graphMode": graph, "textMode": "auto", "orientation": "auto"}}


def ts(pid, title, targets, x, y, w, h, unit=None, min_=None, max_=None, stack=False):
    fc = {"custom": {"lineWidth": 1, "fillOpacity": 10, "showPoints": "never"}}
    if stack:
        fc["custom"]["stacking"] = {"mode": "normal", "group": "A"}
    if unit:
        fc["unit"] = unit
    if min_ is not None:
        fc["min"] = min_
    if max_ is not None:
        fc["max"] = max_
    return {"id": pid, "type": "timeseries", "title": title, "datasource": PROM, "targets": targets,
            "gridPos": {"x": x, "y": y, "w": w, "h": h}, "fieldConfig": {"defaults": fc, "overrides": []},
            "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                        "tooltip": {"mode": "multi", "sort": "desc"}}}


def gauge(pid, title, targets, x, y, w, h, unit="percent", thresholds=None):
    steps = thresholds or [{"color": "green", "value": None}, {"color": "orange", "value": 75}, {"color": "red", "value": 90}]
    return {"id": pid, "type": "gauge", "title": title, "datasource": PROM, "targets": targets,
            "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "fieldConfig": {"defaults": {"unit": unit, "min": 0, "max": 100, "thresholds": {"mode": "absolute", "steps": steps}}, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "showThresholdLabels": False, "showThresholdMarkers": True}}


def bargauge(pid, title, targets, x, y, w, h, unit=None):
    fc = {"thresholds": {"mode": "absolute", "steps": [{"color": "blue", "value": None}]}}
    if unit:
        fc["unit"] = unit
    return {"id": pid, "type": "bargauge", "title": title, "datasource": PROM, "targets": targets,
            "gridPos": {"x": x, "y": y, "w": w, "h": h}, "fieldConfig": {"defaults": fc, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "orientation": "horizontal", "displayMode": "gradient", "showUnfilled": True,
                        "valueMode": "color", "namePlacement": "left", "sizing": "auto", "minVizHeight": 14, "minVizWidth": 8}}


def table(pid, title, targets, x, y, w, h, exclude=()):
    hide = {k: True for k in ("Time", "__name__", "job", "instance", "host", "stack", "app", "Value") + tuple(exclude)}
    return {"id": pid, "type": "table", "title": title, "datasource": PROM, "targets": targets,
            "gridPos": {"x": x, "y": y, "w": w, "h": h}, "fieldConfig": {"defaults": {}, "overrides": []},
            "transformations": [{"id": "labelsToFields", "options": {}}, {"id": "organize", "options": {"excludeByName": hide}}],
            "options": {"showHeader": True}}


def logs(pid, title, expr, x, y, w, h):
    return {"id": pid, "type": "logs", "title": title, "datasource": LOKI,
            "targets": [{"datasource": LOKI, "expr": expr, "refId": "A"}],
            "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending",
                        "dedupStrategy": "none", "enableLogDetails": True, "prettifyLogMessage": False}}


def dashboard(uid, title, tags, panels):
    return {"annotations": {"list": []}, "editable": True, "graphTooltip": 1, "links": [], "panels": panels,
            "refresh": "30s", "schemaVersion": 39, "tags": tags, "templating": {"list": []},
            "time": {"from": "now-6h", "to": "now"}, "timezone": "browser", "title": title, "uid": uid, "version": 1}


def build():
    p, y = [], 0
    p.append(row(1, "At a glance", y)); y += 1
    p.append(stat(2, "Ingress probe (docs.lab.gn.al)", [q(f"probe_success{{{PROBE}}}")], 0, y, 3, 4, mappings=UPDOWN, color_mode="background", thresholds=GREEN_ON_1))
    p.append(stat(3, "Probe latency", [q(f"probe_duration_seconds{{{PROBE}}}")], 3, y, 3, 4, unit="s", decimals=2,
                  thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 1}, {"color": "red", "value": 3}]))
    p.append(stat(4, "TLS days remaining", [q(f"(probe_ssl_earliest_cert_expiry{{{PROBE}}} - time()) / 86400")], 6, y, 3, 4, unit="d", decimals=0,
                  thresholds=[{"color": "red", "value": None}, {"color": "orange", "value": 14}, {"color": "green", "value": 30}]))
    p.append(stat(5, "Exporter scrape", [q('up{job="paperless"}')], 9, y, 3, 4, mappings=UPDOWN, color_mode="background", thresholds=GREEN_ON_1))
    p.append(stat(6, "Documents", [q("paperless_statistics_documents_total")], 12, y, 3, 4, decimals=0, graph="area"))
    p.append(stat(7, "Inbox (untagged)", [q("paperless_statistics_documents_inbox_count")], 15, y, 3, 4, decimals=0,
                  thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 10}]))
    p.append(stat(8, "Tags / correspondents / types", [q("paperless_statistics_tag_count", "tags"), q("paperless_statistics_correspondent_count", "correspondents", ref="B"),
                                                      q("paperless_statistics_document_type_count", "types", ref="C")], 18, y, 3, 4, decimals=0))
    p.append(gauge(9, "Media volume used (NAS)", [q("(1 - paperless_status_storage_available_bytes / paperless_status_storage_total_bytes) * 100")], 21, y, 3, 4))
    y += 4
    p.append(row(10, "Health (Paperless /api/status/)", y)); y += 1
    for i, (name, metric) in enumerate([("Database", "paperless_status_database_status"), ("Redis / broker", "paperless_status_redis_status"),
                                        ("Celery workers", "paperless_status_celery_status"), ("Search index", "paperless_status_index_status"),
                                        ("Classifier", "paperless_status_classifier_status"), ("Sanity check", "paperless_status_sanity_check_status")]):
        p.append(stat(11 + i, name, [q(metric)], i * 4, y, 4, 3, mappings=OKFAIL, color_mode="background", thresholds=GREEN_ON_1))
    y += 3
    p.append(stat(17, "Unapplied DB migrations", [q("paperless_status_database_unapplied_migrations")], 0, y, 4, 3, decimals=0, thresholds=RED_ON_1, color_mode="background"))
    p.append(stat(18, "Last sanity check", [q("paperless_status_sanity_check_last_run_timestamp_seconds * 1000")], 4, y, 5, 3, unit="dateTimeFromNow"))
    p.append(stat(19, "Classifier last trained", [q("paperless_status_classifier_last_trained_timestamp_seconds * 1000")], 9, y, 5, 3, unit="dateTimeFromNow"))
    p.append(stat(20, "Index last modified", [q("paperless_status_index_last_modified_timestamp_seconds * 1000")], 14, y, 5, 3, unit="dateTimeFromNow"))
    p.append(stat(21, "Exporter warnings (24h)", [q("sum(increase(paperless_warnings_total[24h]))")], 19, y, 5, 3, decimals=0,
                  thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 1}]))
    y += 3
    p.append(row(30, "Consumption (API uploads from the scanner and drive-migrate, consume folder, mail)", y)); y += 1
    p.append(ts(31, "Recent tasks by status (Paperless keeps the last ~1000)", [q("count by (status) (paperless_task_status == 1)", "{{status}}")], 0, y, 12, 7, min_=0))
    p.append(ts(32, "Documents total", [q("paperless_statistics_documents_total", "documents")], 12, y, 12, 7, min_=0))
    y += 7
    p.append(table(33, "Failed tasks (file name)", [q('paperless_task_filename and on(id) (paperless_task_status{status="failure"} == 1)', instant=True, fmt="table")], 0, y, 24, 7, exclude=("id",)))
    y += 7
    p.append(row(40, "Library", y)); y += 1
    p.append(bargauge(41, "Documents per tag (top 15)", [q("topk(15, paperless_tag_document_count * on(id) group_left(name) paperless_tag_info)", "{{name}}", instant=True)], 0, y, 12, 10))
    p.append(bargauge(42, "Documents per storage path (top 15)", [q("topk(15, paperless_storage_path_document_count * on(id) group_left(name) paperless_storage_path_info)", "{{name}}", instant=True)], 12, y, 12, 10))
    y += 10
    p.append(row(50, "Database (postgres-exporter) and host", y)); y += 1
    p.append(ts(51, "Postgres connections", [q('pg_stat_database_numbackends{job="paperless-postgres",datname!~"template.*|postgres"}', "{{datname}}")], 0, y, 8, 7, min_=0))
    p.append(ts(52, "Database size", [q('pg_database_size_bytes{job="paperless-postgres",datname!~"template.*|postgres"}', "{{datname}}")], 8, y, 8, 7, unit="bytes", min_=0))
    p.append(ts(53, "kaiser CPU (seen from the stack's node-exporter)", [q('(1 - avg(rate(node_cpu_seconds_total{job="paperless-node",mode="idle"}[5m]))) * 100', "cpu %")], 16, y, 8, 7, unit="percent", min_=0, max_=100))
    y += 7
    p.append(row(60, "Logs (VPS Loki, stack=cn-paperless)", y)); y += 1
    p.append(logs(61, "Consumer / OCR / errors", '{stack="cn-paperless"} |~ "(?i)error|warning|consum|ocr|task"', 0, y, 24, 10))
    return dashboard("paperless", "Paperless-ngx (cn-paperless)", ["homelab", "cn-paperless", "paperless"], p)


def main():
    root = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT.parent.parent / "cn-root-docker"
    out = root / "tailnet" / "grafana" / "provisioning" / "dashboards"
    if not out.is_dir():
        sys.exit(f"cn-root-docker provisioning dir not found: {out}")
    d = build()
    ids = [x["id"] for x in d["panels"]]
    assert len(ids) == len(set(ids)), "duplicate panel ids"
    (out / "paperless.json").write_text(json.dumps(d, indent=2) + "\n")
    print(f"wrote {out / 'paperless.json'} ({len(ids)} panels)")


if __name__ == "__main__":
    main()
