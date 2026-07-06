"""Tests for the OG deep-link routes and Pub/Sub envelope parsing in main.py.

Run: python3 test_og.py          (needs flask; no pytest, no GCP, no Playwright)

Storage + Firestore are faked by monkeypatching the module-level helpers
(_cards_bucket, _fetch_card_doc). main.py imports duckdb/jinja2/playwright
lazily inside the render path, so importing it here needs only flask.
"""

from __future__ import annotations

import base64
import json
import unittest
from unittest import mock

import main


FAKE_GENERATION = 1751700000000000


class FakeBlob:
    def __init__(self, data: bytes | None):
        self._data = data
        self.generation = FAKE_GENERATION

    def exists(self) -> bool:
        return self._data is not None

    def download_as_bytes(self) -> bytes:
        if self._data is None:
            raise RuntimeError("blob missing — route must check exists() first")
        return self._data


class FakeBucket:
    """Maps blob name → bytes; anything else is a missing blob."""

    def __init__(self, blobs: dict[str, bytes] | None = None):
        self._blobs = blobs or {}

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(self._blobs.get(name))

    def get_blob(self, name: str) -> FakeBlob | None:
        # Mirrors google-cloud-storage: None for a missing blob.
        data = self._blobs.get(name)
        return FakeBlob(data) if data is not None else None


PNG_BYTES = b"\x89PNG\r\n\x1a\nfakepngdata"
CARD_ID = "ROB-041726-001"
CARD_DOC = {
    "card_id": CARD_ID,
    "headline": 'Figure & BMW expand "humanoid" pilot <2026>',
    "subtitle": "Direct: pilot scales to body-shop tasks & night shifts",
}


class OgRouteTests(unittest.TestCase):
    def setUp(self):
        main.app.config["TESTING"] = True
        self.client = main.app.test_client()
        main._card_doc_cache.clear()
        # Default fakes: card doc + PNG both present. Individual tests override.
        self._patches = [
            mock.patch.object(main, "_fetch_card_doc",
                              lambda cid: CARD_DOC if cid == CARD_ID else None),
            mock.patch.object(main, "_cards_bucket",
                              lambda: FakeBucket({f"cards/{CARD_ID}.png": PNG_BYTES})),
            mock.patch.object(main, "STORAGE_CARDS_PREFIX", "cards"),
            mock.patch.object(main, "OG_ONLY", False),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(mock.patch.stopall)

    # ── /card/<id> ──────────────────────────────────────────────────

    def test_valid_card_page(self):
        resp = self.client.get(f"/card/{CARD_ID}")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.content_type.startswith("text/html"))
        self.assertEqual(resp.headers["Cache-Control"], "public, max-age=300")
        body = resp.get_data(as_text=True)
        # Headline is escaped: &, <, > and quotes must not appear raw.
        self.assertIn(
            'og:title" content="Figure &amp; BMW expand &quot;humanoid&quot; '
            "pilot &lt;2026&gt;",
            body,
        )
        self.assertIn(
            f'og:image" content="https://robotics.arboryx.ai/card-img/{CARD_ID}.png?g={FAKE_GENERATION}"',
            body,
        )
        self.assertIn('og:image:width" content="2400"', body)
        self.assertIn('og:image:height" content="1260"', body)
        self.assertIn(
            f'og:url" content="https://robotics.arboryx.ai/card/{CARD_ID}"', body
        )
        self.assertIn('twitter:card" content="summary_large_image"', body)
        self.assertIn("Direct: pilot scales", body)
        # Human redirect: meta refresh + JS fallback + plain link.
        self.assertIn(f'content="0;url=/?card={CARD_ID}"', body)
        self.assertIn(f'location.replace("/?card={CARD_ID}")', body)
        self.assertIn(f'href="/?card={CARD_ID}"', body)

    def test_firestore_miss_but_png_exists_gets_generic_page(self):
        mock.patch.object(main, "_fetch_card_doc", lambda cid: None).start()
        resp = self.client.get(f"/card/{CARD_ID}")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn(main.GENERIC_TITLE, body)
        self.assertIn(f"/card-img/{CARD_ID}.png", body)

    def test_firestore_error_but_png_exists_gets_generic_page(self):
        def boom(cid):
            raise RuntimeError("firestore down")
        mock.patch.object(main, "_fetch_card_doc", boom).start()
        resp = self.client.get(f"/card/{CARD_ID}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(main.GENERIC_TITLE, resp.get_data(as_text=True))

    def test_doc_and_png_both_missing_redirects_home(self):
        mock.patch.object(main, "_fetch_card_doc", lambda cid: None).start()
        mock.patch.object(main, "_cards_bucket", lambda: FakeBucket()).start()
        resp = self.client.get(f"/card/{CARD_ID}")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], main.CANONICAL_ORIGIN)

    def test_bad_ids_rejected(self):
        # Path traversal: Flask's default <converter> won't match slashes, so
        # ../ never reaches the handler — expect 404 from routing or the regex.
        for bad in ("..%2F..%2Fetc%2Fpasswd", "a" * 65, "ROB_041726", "id with space"):
            resp = self.client.get(f"/card/{bad}")
            self.assertEqual(resp.status_code, 404, f"id {bad!r} not rejected")
        resp = self.client.get("/card/../render")
        self.assertNotEqual(resp.status_code, 200)

    # ── /card-img/<id>.png ──────────────────────────────────────────

    def test_card_img_streams_png(self):
        resp = self.client.get(f"/card-img/{CARD_ID}.png")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content_type, "image/png")
        self.assertEqual(resp.headers["Cache-Control"], "public, max-age=3600, must-revalidate")
        self.assertEqual(resp.get_data(), PNG_BYTES)

    def test_card_img_missing_blob_404(self):
        resp = self.client.get("/card-img/ROB-999999-999.png")
        self.assertEqual(resp.status_code, 404)

    def test_card_img_bad_id_404(self):
        self.assertEqual(self.client.get("/card-img/a b.png").status_code, 404)
        self.assertEqual(
            self.client.get(f"/card-img/{'a' * 65}.png").status_code, 404
        )

    # ── OG_ONLY gating ──────────────────────────────────────────────

    def test_og_only_disables_render_routes(self):
        mock.patch.object(main, "OG_ONLY", True).start()
        self.assertEqual(
            self.client.post("/render", json={"card_id": CARD_ID}).status_code, 404
        )
        self.assertEqual(self.client.post("/render-batch", json={}).status_code, 404)
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        # OG routes still serve.
        self.assertEqual(self.client.get(f"/card/{CARD_ID}").status_code, 200)

    def test_og_only_unset_keeps_render_routes_registered(self):
        # OG_ONLY=False (setUp default): /render must get past the gate and
        # into normal handling (400 on missing card_id — not 404).
        resp = self.client.post("/render", json={})
        self.assertEqual(resp.status_code, 400)


class ParseBatchBodyTests(unittest.TestCase):
    """_parse_batch_body: plain JSON passes through; Pub/Sub push envelopes
    are base64-decoded; anything undecodable → {} (full-backlog mode)."""

    @staticmethod
    def _envelope(payload) -> dict:
        data = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
        return {"message": {"data": data, "messageId": "1"}, "subscription": "s"}

    def test_plain_json_passthrough(self):
        body = {"limit": 5, "force": True}
        self.assertEqual(main._parse_batch_body(body), body)

    def test_empty_body(self):
        self.assertEqual(main._parse_batch_body({}), {})

    def test_envelope_decodes_params(self):
        self.assertEqual(
            main._parse_batch_body(self._envelope({"since_days": 7, "limit": 3})),
            {"since_days": 7, "limit": 3},
        )

    def test_envelope_with_ingest_done_event(self):
        # The actual ingest-done payload has no render params → full backlog.
        parsed = main._parse_batch_body(
            self._envelope({"sector": "Robotics", "written": 4, "ts": "2026-07-05T06:10:00Z"})
        )
        self.assertIsNone(parsed.get("limit"))
        self.assertIsNone(parsed.get("from_id"))
        self.assertFalse(bool(parsed.get("force")))

    def test_envelope_garbage_data(self):
        self.assertEqual(
            main._parse_batch_body({"message": {"data": "not-base64!!!"}}), {}
        )

    def test_envelope_non_json_data(self):
        data = base64.b64encode(b"plain text").decode("ascii")
        self.assertEqual(main._parse_batch_body({"message": {"data": data}}), {})

    def test_envelope_json_but_not_object(self):
        data = base64.b64encode(b"[1, 2, 3]").decode("ascii")
        self.assertEqual(main._parse_batch_body({"message": {"data": data}}), {})

    def test_envelope_missing_data(self):
        self.assertEqual(main._parse_batch_body({"message": {}}), {})

    def test_non_dict_body(self):
        self.assertEqual(main._parse_batch_body(["not", "a", "dict"]), {})

    def test_message_key_not_dict_passthrough(self):
        # "message" that isn't a dict is just a user field, not an envelope.
        body = {"message": "hello", "limit": 2}
        self.assertEqual(main._parse_batch_body(body), body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
