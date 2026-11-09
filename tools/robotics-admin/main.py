"""robotics-admin — data editor API used by robotics.arboryx.ai/admin.

The page calls this service's own run.app URL directly (CORS, origin
allowlisted) — NOT through a Firebase Hosting rewrite — so there is exactly
one network path in, and the client IP is read from the right end of
X-Forwarded-For, the part Google's front end appends (anything the caller
sent is in front and ignored; see _client_ip). Every request must carry a Firebase ID token for an allowed
admin email AND come from an allowed IP; both lists live in the private
Firestore doc ckg_admin/<sector-lowercase> ({emails: [...], ips: [...]}),
editable in the Firebase console (no redeploy). Scales to zero when idle.

Reads use a cached copy of the authoritative DuckDB, refreshed whenever the
GCS object's generation changes. Each edit (one at a time, process lock):
  pull a fresh copy (generation pinned) → src/admin_ops.apply_edit (validates
  `expect`, audits) → push with if_generation_match (a concurrent ingest makes
  it fail, nothing overwritten) → rewrite the touched card docs
  and the embeddings of touched cards (graph doc/insights refresh at the next
  ingest; the web app draws the map from the card docs).
A failed edit discards its working copy — production is untouched.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
import time
from typing import Any

import duckdb
from flask import Flask, jsonify, request

from src import admin_ops, duckdb_sync
from src.config import load_config

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":%(message)s}')
log = logging.getLogger("robotics-admin")
app = Flask(__name__)
_ORIGINS = {o.strip() for o in os.environ.get("ADMIN_ALLOWED_ORIGINS", "https://robotics.arboryx.ai").split(",")
            if o.strip()}


@app.before_request
def _preflight():
    if request.method == "OPTIONS":   # CORS preflight carries no credentials — answer, never authorize
        return "", 204


@app.after_request
def _cors(resp):
    origin = request.headers.get("Origin")
    if origin in _ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Max-Age"] = "600"
    return resp
CFG = load_config()
_WRITE_LOCK = threading.Lock()
_READ_LOCK = threading.Lock()
_READ = {"path": "/tmp/admin_read/robotics.duckdb", "generation": None}
_ACCESS: dict[str, Any] = {"at": 0.0, "emails": set(), "ips": set()}
_ACCESS_TTL = 60


def _log(event: str, **fields: Any) -> None:
    level = logging.ERROR if event.endswith("_failed") else logging.INFO
    log.log(level, json.dumps({"event": event, **fields}, default=str))


def _firestore():
    from google.cloud import firestore  # noqa: PLC0415

    fs = CFG.firestore
    kw = {"database": fs.database} if fs.database and fs.database != "(default)" else {}
    return firestore.Client(project=fs.project or None, **kw)


def _access() -> dict[str, Any]:
    if time.time() - _ACCESS["at"] > _ACCESS_TTL:
        doc = _firestore().collection("ckg_admin").document(CFG.sector.lower()).get().to_dict() or {}
        _ACCESS.update(at=time.time(), emails={e.strip().lower() for e in doc.get("emails", [])},
                       ips={i.strip() for i in doc.get("ips", [])})
    return _ACCESS


# Google's front end APPENDS the address it observed to X-Forwarded-For;
# whatever the caller sent stays in front. So only the entry ADMIN_XFF_HOPS
# from the right is trusted (1 = last entry, the observed client on a direct
# run.app call). Counting from the right never lands on a caller-supplied
# entry; a wrong count only locks admins OUT (fail closed) — the denial log
# shows the full chain to calibrate. NEVER allowlist a Google-owned address
# seen in that log (e.g. a front-end hop): it would admit every caller.
_XFF_HOPS = max(1, int(os.environ.get("ADMIN_XFF_HOPS", "1")))


def _client_ip() -> str | None:
    chain = [p.strip() for p in request.headers.get("X-Forwarded-For", "").split(",") if p.strip()]
    return chain[-_XFF_HOPS] if len(chain) >= _XFF_HOPS else None


class Denied(Exception):
    pass


def _authorize() -> tuple[str, str]:
    """(email, ip) of an allowed admin, else Denied. The email check is the
    primary gate (verified Google sign-in); the IP check narrows it further."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise Denied("sign in required")
    import firebase_admin  # noqa: PLC0415
    from firebase_admin import auth  # noqa: PLC0415

    if not firebase_admin._apps:
        firebase_admin.initialize_app(options={"projectId": CFG.firestore.project or None})
    try:
        claims = auth.verify_id_token(header[7:])
    except Exception as exc:  # noqa: BLE001
        raise Denied(f"invalid sign-in: {type(exc).__name__}") from exc
    email = (claims.get("email") or "").lower()
    acc = _access()
    if not claims.get("email_verified") or email not in acc["emails"]:
        raise Denied(f"{email or 'this account'} is not an admin")
    ip = _client_ip()
    if not ip or ip not in acc["ips"]:
        raise Denied(f"IP not allowed ({ip or 'unknown'}) — add it to ckg_admin/{CFG.sector.lower()}.ips")
    return email, ip


def _guard(fn):
    def wrapper(*a, **kw):
        try:
            who = _authorize()
        except Denied as d:
            _log("admin_denied", reason=str(d), ip=_client_ip(),
                 xff=request.headers.get("X-Forwarded-For", ""))
            return jsonify({"ok": False, "error": str(d)}), 403
        try:
            return fn(who, *a, **kw)
        except admin_ops.EditConflict as exc:
            return jsonify({"ok": False, "error": str(exc), "conflict": True}), 409
        except admin_ops.EditError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
    wrapper.__name__ = fn.__name__
    return wrapper


def _read_con() -> duckdb.DuckDBPyConnection:
    """Read-only connection on a cached copy, re-pulled when GCS changed."""
    from google.cloud import storage  # noqa: PLC0415

    blob = storage.Client().bucket(os.environ["DUCKDB_GCS_BUCKET"]).blob(os.path.basename(CFG.duckdb_path))
    blob.reload()
    with _READ_LOCK:
        if blob.generation != _READ["generation"] or not os.path.exists(_READ["path"]):
            os.makedirs(os.path.dirname(_READ["path"]), exist_ok=True)
            tmp = _READ["path"] + ".part"
            blob.download_to_filename(tmp, if_generation_match=blob.generation)
            os.replace(tmp, _READ["path"])
            _READ["generation"] = blob.generation
    con = duckdb.connect(_READ["path"], read_only=True)
    return con


def _vocab() -> set[str]:
    return {v["name"] for v in CFG.vocab.get("vocabulary", [])}


def _refresh_firestore(cards: list[str]) -> dict[str, Any]:
    """Touched card docs + entities collection, and the touched cards' embeddings."""
    out: dict[str, Any] = {}
    try:   # touched cards only: Hosting gives the request 60s, a full export takes longer
        from src.firestore_export import refresh_cards  # noqa: PLC0415

        out["firestore"] = refresh_cards(CFG, cards) if cards else {"cards": "none touched"}
    except Exception as exc:  # noqa: BLE001
        out["firestore_error"] = f"{type(exc).__name__}: {exc}"
        _log("admin_refresh_failed", error=out["firestore_error"])
    if cards:
        try:
            from google import genai  # noqa: PLC0415

            from src import embed_sync  # noqa: PLC0415

            fs = _firestore()
            items = fs.collection(CFG.firestore_export.collection).document(CFG.firestore_export.catalysts_subpath) \
                .collection("items")
            con = duckdb.connect(CFG.duckdb_path, read_only=True)
            try:
                out["embed"] = embed_sync.sync(con, CFG.sector, fs, items,
                                               embed_sync.vectors_ref(fs, CFG.firestore_export.collection),
                                               genai.Client(api_key=CFG.extraction.api_key), cards)
            finally:
                con.close()
        except Exception as exc:  # noqa: BLE001
            out["embed_error"] = f"{type(exc).__name__}: {exc}"
            _log("admin_embed_failed", error=out["embed_error"])
    return out


def _write(apply) -> tuple[Any, int]:
    """Run `apply(con)` on a fresh, generation-pinned copy; push only if it
    succeeded. Returns (json, status)."""
    with _WRITE_LOCK:
        work = tempfile.mkdtemp(prefix="admin_edit_")
        path = os.path.join(work, os.path.basename(CFG.duckdb_path))
        try:
            duckdb_sync.pull(path)
            con = duckdb.connect(path)
            try:
                admin_ops.ensure_schema(con)
                result = apply(con)
                con.execute("CHECKPOINT")
            finally:
                con.close()
            pushed = duckdb_sync.push(path)
            if not pushed.get("pushed"):
                return {"ok": False, "conflict": True,
                        "error": f"not saved ({pushed.get('reason')}): the database changed meanwhile (an ingest "
                                 f"ran) — reload and try again"}, 409
            # The export reads cfg.duckdb_path: point it at the copy just pushed.
            os.makedirs(os.path.dirname(CFG.duckdb_path) or ".", exist_ok=True)
            shutil.copyfile(path, CFG.duckdb_path)
            refresh = _refresh_firestore(result.get("cards", []))
            _log("admin_edit", op=result.get("op"), edit_id=result.get("edit_id"), cards=result.get("cards"))
            return {"ok": True, **result, **refresh}, 200
        finally:
            shutil.rmtree(work, ignore_errors=True)


# ── Backend activity (banner) ──────────────────────────────────────
# Other writers of the same data: the CKG ingest (DuckDB + CKG Firestore),
# render (reads DuckDB, writes images), and Arboryx jobs (findings + dedup
# sweep) — all started by Cloud Scheduler. Edits stay safe either way (the
# generation-checked push refuses to overwrite), but the admin is warned to
# edit outside those windows so their next run starts from the corrected data.
_STATUS: dict[str, Any] = {"at": 0.0, "data": None}
_STATUS_TTL = 30
_SOON_MIN = 60          # warn when a scheduled run starts within this many minutes
_RUN_WINDOW_MIN = 40    # a job started this recently is treated as possibly still running


def _gcp_get(url: str, body: dict | None = None) -> dict:
    import google.auth  # noqa: PLC0415
    import google.auth.transport.requests as tr  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(tr.Request())
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"},
                                 method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def _parse_ts(ts: str | None):
    from datetime import datetime  # noqa: PLC0415
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _last_events(service: str, events: list[str], minutes: int) -> dict[str, Any]:
    """Newest timestamp per event name for `service` in the last `minutes`."""
    from datetime import datetime, timedelta, timezone  # noqa: PLC0415

    since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    ev = " OR ".join(f'"{e}"' for e in events)
    data = _gcp_get("https://logging.googleapis.com/v2/entries:list", {
        "resourceNames": [f"projects/{CFG.firestore.project}"], "orderBy": "timestamp desc", "pageSize": 20,
        "filter": f'resource.labels.service_name="{service}" AND jsonPayload.msg.event=({ev}) AND timestamp>="{since}"'})
    out: dict[str, Any] = {}
    for e in data.get("entries", []):
        name = ((e.get("jsonPayload") or {}).get("msg") or {}).get("event")
        if name and name not in out:
            out[name] = _parse_ts(e.get("timestamp"))
    return out


def _backend_status() -> dict[str, Any]:
    from datetime import datetime, timezone  # noqa: PLC0415

    if _STATUS["data"] is not None and time.time() - _STATUS["at"] < _STATUS_TTL:
        return _STATUS["data"]
    now = datetime.now(timezone.utc)
    running, soon, jobs, errors = [], [], [], []
    try:
        for loc in os.environ.get("ADMIN_SCHEDULER_LOCATIONS", "us-central1").split(","):
            data = _gcp_get(f"https://cloudscheduler.googleapis.com/v1/projects/{CFG.firestore.project}"
                            f"/locations/{loc.strip()}/jobs")
            for j in data.get("jobs", []):
                if j.get("state") != "ENABLED":
                    continue
                name = j["name"].rsplit("/", 1)[-1]
                nxt, last = _parse_ts(j.get("scheduleTime")), _parse_ts(j.get("lastAttemptTime"))
                row = {"job": name, "schedule": j.get("schedule"), "time_zone": j.get("timeZone"),
                       "next_run": nxt.isoformat() if nxt else None,
                       "minutes_until": round((nxt - now).total_seconds() / 60) if nxt else None,
                       "minutes_since_start": round((now - last).total_seconds() / 60) if last else None}
                jobs.append(row)
                if row["minutes_until"] is not None and 0 <= row["minutes_until"] <= _SOON_MIN:
                    soon.append(row)
                # The CKG ingest is checked precisely from its logs below.
                if name != "robotics-ingest-daily" and row["minutes_since_start"] is not None \
                        and row["minutes_since_start"] <= _RUN_WINDOW_MIN:
                    running.append({"what": name, "minutes_since_start": row["minutes_since_start"], "precise": False})
    except Exception as exc:  # noqa: BLE001
        errors.append(f"scheduler: {type(exc).__name__}: {exc}")
    for service, start, ends, label in (("robotics-ingest", "ingest_start", ["ingest_done", "ingest_failed"], "CKG ingest"),
                                        ("robotics-render", "duckdb_pull", ["batch_ok"], "image render")):
        try:
            ev = _last_events(service, [start, *ends], 90)
            started, ended = ev.get(start), max([ev[e] for e in ends if ev.get(e)], default=None)
            if started and (ended is None or ended < started):
                running.append({"what": label, "minutes_since_start": round((now - started).total_seconds() / 60),
                                "precise": True})
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{service} logs: {type(exc).__name__}: {exc}")
    data = {"checked_at": now.isoformat(), "running": running, "soon": sorted(soon, key=lambda r: r["minutes_until"]),
            "jobs": sorted(jobs, key=lambda r: (r["minutes_until"] is None, r["minutes_until"] or 0)), "errors": errors}
    _STATUS.update(at=time.time(), data=data)
    return data


# ── Routes ─────────────────────────────────────────────────────────

@app.get("/admin/api/me")
@_guard
def me(who):
    return jsonify({"ok": True, "email": who[0], "ip": who[1], "sector": CFG.sector,
                    "rel_types": sorted(_vocab()), "statuses": list(admin_ops.EDGE_STATUSES),
                    "link_relations": list(admin_ops.LINK_RELATIONS)})


@app.get("/admin/api/status")
@_guard
def status(_who):
    return jsonify({"ok": True, **_backend_status()})


@app.get("/admin/api/entities")
@_guard
def entities(_who):
    con = _read_con()
    try:
        return jsonify({"ok": True, "entities": admin_ops.list_entities(con, request.args.get("q", ""),
                                                                        int(request.args.get("limit", 200)))})
    finally:
        con.close()


@app.get("/admin/api/entity/<int:eid>")
@_guard
def entity(_who, eid):
    con = _read_con()
    try:
        return jsonify({"ok": True, **admin_ops.entity_detail(con, eid)})
    finally:
        con.close()


@app.get("/admin/api/scan")
@_guard
def scan(_who):
    con = _read_con()
    try:
        return jsonify({"ok": True, **admin_ops.scan(con)})
    finally:
        con.close()


@app.get("/admin/api/edits")
@_guard
def edits(_who):
    con = _read_con()
    try:
        admin_tables = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
        return jsonify({"ok": True, "edits": admin_ops.edits(con) if "admin_edits" in admin_tables else []})
    finally:
        con.close()


@app.post("/admin/api/edit")
@_guard
def edit(who):
    body = request.get_json(silent=True) or {}
    op, payload = body.get("op"), dict(body.get("payload") or {})
    busy = _backend_status()
    if busy["running"]:   # recorded on the audit row: edited while a backend run was in flight
        payload["_backend_running"] = [r["what"] for r in busy["running"]]
    body_out, code = _write(lambda con: admin_ops.apply_edit(con, op, payload, email=who[0], ip=who[1],
                                                             vocab=_vocab()))
    if busy["running"]:
        body_out["warning"] = ("Saved while " + ", ".join(r["what"] for r in busy["running"]) + " was running — "
                               "recheck this record after it finishes.")
    return jsonify(body_out), code


@app.post("/admin/api/revert")
@_guard
def revert(who):
    edit_id = (request.get_json(silent=True) or {}).get("edit_id")
    body_out, status = _write(lambda con: admin_ops.revert_edit(con, edit_id, email=who[0], ip=who[1],
                                                                vocab=_vocab()))
    return jsonify(body_out), status


@app.post("/admin/api/suggest")
@_guard
def suggest(_who):
    """LLM proposals (no write): which family member each selected edge's
    endpoint really refers to, judged from the card's own text."""
    body = request.get_json(silent=True) or {}
    from google import genai  # noqa: PLC0415

    con = _read_con()
    try:
        out = admin_ops.suggest_repoints(con, genai.Client(api_key=CFG.extraction.api_key), CFG,
                                         int(body["entity_id"]), [int(r) for r in body.get("rel_ids", [])])
        return jsonify({"ok": True, "suggestions": out})
    finally:
        con.close()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8082)))
