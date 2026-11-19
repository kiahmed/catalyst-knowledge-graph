"""Admin UI tests, server side (inside the ingest image): the REAL admin Flask app in TEST MODE.

    python tests/ui/admin_server.py <port>

- Data: a read-only snapshot of prod is pulled ONCE into a local file. Every save ("pull → apply → push") then
  works on that local file only: the pull copies it, the push copies the result back. Nothing reaches GCS.
- Safety net: right after the snapshot, DUCKDB_GCS_BUCKET is pointed at a bucket that doesn't exist, so any GCS
  call this file failed to stub errors out instead of writing to prod.
- Stubbed (external services): sign-in check (a fake admin user), Firestore card/entity refresh + embeddings,
  the review queue, the backend-activity banner (Cloud Logging).
- Also serves frontend/admin.html + assets, with a fake signed-in Firebase user (fake /assets/auth.js) and
  admin-config.js pointing at this server.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "robotics-admin"))
sys.path.insert(0, str(ROOT))

from src import duckdb_sync  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8791
WORK = Path(tempfile.mkdtemp(prefix="admin_ui_test_"))
MASTER = WORK / "robotics.duckdb"                 # the ONLY database this server ever writes
duckdb_sync.pull(str(MASTER))                     # one read of prod
os.environ["DUCKDB_GCS_BUCKET"] = "ckg-test-no-such-bucket-do-not-create"   # any missed GCS call now fails
os.environ["DUCKDB_PATH"] = str(WORK / "export_copy.duckdb")

import duckdb  # noqa: E402
import main  # noqa: E402   (tools/robotics-admin/main.py — the real admin app)
from flask import Response, send_from_directory  # noqa: E402

from src import review  # noqa: E402


def _local_pull(path: str) -> dict:
    shutil.copyfile(MASTER, path)
    return {"pulled": True, "reason": "local test copy"}


def _local_push(path: str) -> dict:
    shutil.copyfile(path, MASTER)
    return {"pushed": True, "reason": "local test copy"}


duckdb_sync.pull = main.duckdb_sync.pull = _local_pull
duckdb_sync.push = main.duckdb_sync.push = _local_push
main._authorize = lambda: ("ui-test@local", "127.0.0.1")
main._read_con = lambda: duckdb.connect(str(MASTER), read_only=True)
main._refresh_firestore = lambda cards: {"firestore": "skipped (UI test)", "embed": "skipped (UI test)"}
main._backend_status = lambda: {"running": [], "soon": [], "errors": []}
main._auto_resolve = lambda *a, **k: 0
review.list_open = lambda fs: []
review.count_open = lambda fs: 0
main._firestore = lambda: None

FRONTEND = ROOT / "frontend"
FAKE_AUTH = """window.RoboticsAuth = { requireSignIn: (cfg, cb) => cb({ email: 'ui-test@local', getIdToken: async () => 'test' }),
  deferredGate: (cfg, cb) => cb({ email: 'ui-test@local', getIdToken: async () => 'test' }), noteCardOpen: () => {} };"""


@main.app.get("/admin.html")
def _admin_page():
    return send_from_directory(FRONTEND, "admin.html")


@main.app.get("/admin-config.js")
def _admin_config():
    return Response(f'window.ROBOTICS_ADMIN_API = "http://127.0.0.1:{PORT}/admin/api";', mimetype="text/javascript")


@main.app.get("/firebase-config.js")
def _firebase_config():
    return Response("window.FIREBASE_CONFIG = { apiKey: 'test' };", mimetype="text/javascript")


@main.app.get("/assets/auth.js")
def _fake_auth():
    return Response(FAKE_AUTH, mimetype="text/javascript")


@main.app.get("/assets/<path:p>")
def _assets(p):
    return send_from_directory(FRONTEND / "assets", p)


@main.app.get("/__health")
def _health():
    return {"ok": True, "db": str(MASTER)}


if __name__ == "__main__":
    print(f"admin UI test server on :{PORT} — local copy {MASTER} (prod is never written)", flush=True)
    main.app.run(host="0.0.0.0", port=PORT, threaded=False)
