"""Tests for the ingest-done Pub/Sub publish in main.py.

Run: python3 test_publish.py       (needs flask; no pytest, no GCP, no duckdb)

main.py imports src.* and functions_framework at module level; both are
stubbed via sys.modules so importing here needs only flask. The pubsub client
is likewise faked through sys.modules — google-cloud-pubsub is never imported
for real.
"""

from __future__ import annotations

import json
import os
import sys
import types
import unittest
from unittest import mock


def _stub_module(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


class _FakeIngestResult:
    def __init__(self, written: int):
        self.entries_written = written

    def to_json(self):
        return {"entries_written": self.entries_written}


class _FakeConfig:
    sector = "Robotics"
    duckdb_path = "/tmp/fake.duckdb"


# Stub everything main.py pulls in at import time except flask.
_stub_module("functions_framework", http=lambda f: f)
_stub_module("src")
_stub_module("src.config", load_config=lambda: _FakeConfig())
_stub_module("src.duckdb_sync", pull=lambda p: {}, push=lambda p: {})
_stub_module("src.export", export_cards=lambda cfg: {"cards": 0})
_stub_module("src.firestore_export", export_to_firestore=lambda cfg: {"written": 0})
_stub_module("src.ingest", run_ingest=lambda cfg, dry_run, limit: _FakeIngestResult(1))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import main  # noqa: E402


class FakeFuture:
    def __init__(self, message_id="msg-123", exc: Exception | None = None):
        self._message_id = message_id
        self._exc = exc

    def result(self, timeout=None):
        if self._exc:
            raise self._exc
        return self._message_id


class FakePublisher:
    """Records publish calls; constructed count tracked on the class."""

    constructed = 0

    def __init__(self):
        FakePublisher.constructed += 1
        self.calls: list[tuple[str, bytes]] = []
        self.future = FakeFuture()

    def publish(self, topic_path: str, data: bytes):
        self.calls.append((topic_path, data))
        return self.future


class FakeRequest:
    def __init__(self, payload: dict | None = None):
        self._payload = payload or {}

    def get_json(self, silent=False):
        return self._payload


def _install_fake_pubsub():
    """Insert a fake google.cloud.pubsub_v1 tree into sys.modules.
    Returns the FakePublisher class (reset per test)."""
    FakePublisher.constructed = 0
    google = _stub_module("google")
    cloud = _stub_module("google.cloud")
    google.cloud = cloud
    pubsub_v1 = _stub_module("google.cloud.pubsub_v1", PublisherClient=FakePublisher)
    cloud.pubsub_v1 = pubsub_v1
    return FakePublisher


class PublishHelperTests(unittest.TestCase):
    def setUp(self):
        self.publisher_cls = _install_fake_pubsub()
        self._env = mock.patch.dict(
            os.environ,
            {"PUBSUB_DONE_TOPIC": "robotics-ingest-done", "GCP_PROJECT": "test-proj"},
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_publishes_topic_path_and_payload(self):
        # Grab the instance the helper constructs.
        instances: list[FakePublisher] = []
        orig_init = FakePublisher.__init__

        def tracking_init(inst):
            orig_init(inst)
            instances.append(inst)

        with mock.patch.object(FakePublisher, "__init__", tracking_init):
            summary = main._publish_ingest_done("Robotics", 3)

        self.assertEqual(summary, {"ok": True, "message_id": "msg-123"})
        self.assertEqual(len(instances), 1)
        topic_path, data = instances[0].calls[0]
        self.assertEqual(topic_path, "projects/test-proj/topics/robotics-ingest-done")
        payload = json.loads(data.decode("utf-8"))
        self.assertEqual(payload["sector"], "Robotics")
        self.assertEqual(payload["written"], 3)
        self.assertIn("ts", payload)

    def test_topic_unset_skips_without_client(self):
        with mock.patch.dict(os.environ, {"PUBSUB_DONE_TOPIC": ""}):
            summary = main._publish_ingest_done("Robotics", 3)
        self.assertEqual(summary["skipped"], "topic_unset")
        self.assertEqual(FakePublisher.constructed, 0)

    def test_zero_written_skips_without_client(self):
        summary = main._publish_ingest_done("Robotics", 0)
        self.assertEqual(summary["skipped"], "no_new_catalysts")
        self.assertEqual(FakePublisher.constructed, 0)

    def test_publisher_error_returns_ok_false(self):
        def boom(self, topic_path, data):
            raise RuntimeError("pubsub down")

        with mock.patch.object(FakePublisher, "publish", boom):
            summary = main._publish_ingest_done("Robotics", 3)
        self.assertFalse(summary["ok"])
        self.assertIn("pubsub down", summary["error"])

    def test_future_timeout_returns_ok_false(self):
        def slow_publish(self, topic_path, data):
            return FakeFuture(exc=TimeoutError("deadline"))

        with mock.patch.object(FakePublisher, "publish", slow_publish):
            summary = main._publish_ingest_done("Robotics", 3)
        self.assertFalse(summary["ok"])


class RunIngestWiringTests(unittest.TestCase):
    """run_ingest response carries the publish summary; dry runs never publish."""

    def setUp(self):
        _install_fake_pubsub()
        self._env = mock.patch.dict(
            os.environ,
            {"PUBSUB_DONE_TOPIC": "robotics-ingest-done", "GCP_PROJECT": "test-proj"},
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_dry_run_never_publishes(self):
        with mock.patch.object(main, "_publish_ingest_done") as pub:
            payload, status = main.run_ingest(FakeRequest({"dry_run": True}))
        self.assertEqual(status, 200)
        pub.assert_not_called()
        self.assertIsNone(payload["publish"])
        self.assertEqual(FakePublisher.constructed, 0)

    def test_real_run_publishes_and_reports(self):
        payload, status = main.run_ingest(FakeRequest({}))
        self.assertEqual(status, 200)
        self.assertEqual(payload["publish"], {"ok": True, "message_id": "msg-123"})

    def test_publish_failure_does_not_fail_response(self):
        def boom(self, topic_path, data):
            raise RuntimeError("pubsub down")

        with mock.patch.object(FakePublisher, "publish", boom):
            payload, status = main.run_ingest(FakeRequest({}))
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["publish"]["ok"])
        self.assertIn("pubsub down", payload["publish"]["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
