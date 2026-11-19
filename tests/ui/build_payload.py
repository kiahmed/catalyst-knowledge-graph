"""UI tests, step 1 (inside the ingest image): build the site payload from a READ-ONLY copy of prod.

Pulls gs://<bucket>/robotics.duckdb into a temp file, opens it read-only and writes cards.json (the same
shape the Firestore export ships) to the given directory. Never writes to prod.
    python tests/ui/build_payload.py <out_dir>
"""
import dataclasses
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src import duckdb_sync, export  # noqa: E402
from src.config import load_config  # noqa: E402

out = Path(sys.argv[1])
p = os.path.join(tempfile.mkdtemp(), "robotics.duckdb")
duckdb_sync.pull(p)
payload = export.build_payload(dataclasses.replace(load_config(), duckdb_path=p))
(out / "cards.json").write_text(json.dumps(payload, default=str))
print(f"payload: {len(payload['cards'])} cards, {len(payload['graph']['edges'])} graph edges → {out}/cards.json")
