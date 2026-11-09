"""Access-control tests for the admin service (no network: sign-in + allowlist stubbed).

Run: python -m unittest tools/robotics-admin/test_main.py -v   (ingest image + flask, firebase-admin)
"""
from __future__ import annotations

import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

import main  # noqa: E402

ADMIN_IP = "203.0.113.1"     # RFC 5737 documentation addresses
OTHER_IP = "198.51.100.7"


def _token(email="admin@example.com", verified=True):
    fake_auth = types.SimpleNamespace(verify_id_token=lambda t: {"email": email, "email_verified": verified})
    return mock.patch.dict(sys.modules, {"firebase_admin": types.SimpleNamespace(_apps=[1], auth=fake_auth),
                                         "firebase_admin.auth": fake_auth})


class TestAccess(unittest.TestCase):
    def setUp(self):
        main._ACCESS.update(at=9e18, emails={"admin@example.com"}, ips={ADMIN_IP})   # skip the Firestore read
        self.client = main.app.test_client()

    def _me(self, xff, **tok):
        with _token(**tok):
            return self.client.get("/admin/api/me", headers={"Authorization": "Bearer t", "X-Forwarded-For": xff})

    def test_allowed_admin_from_allowed_ip(self):
        r = self._me(ADMIN_IP)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["ip"], ADMIN_IP)

    def test_spoofed_ip_in_front_of_the_real_one_is_ignored(self):
        # A direct caller sends "X-Forwarded-For: <admin ip>"; Google's front end appends the real one.
        r = self._me(f"{ADMIN_IP}, {OTHER_IP}")
        self.assertEqual(r.status_code, 403)
        self.assertIn(OTHER_IP, r.get_json()["error"])

    def test_wrong_or_unverified_email_and_missing_token(self):
        self.assertEqual(self._me(ADMIN_IP, email="intruder@example.com").status_code, 403)
        self.assertEqual(self._me(ADMIN_IP, verified=False).status_code, 403)
        r = self.client.get("/admin/api/me", headers={"X-Forwarded-For": ADMIN_IP})
        self.assertEqual(r.status_code, 403)

    def test_cors_only_for_the_site_origin_and_preflight_never_authorizes(self):
        r = self.client.options("/admin/api/edit", headers={"Origin": "https://robotics.arboryx.ai"})
        self.assertEqual(r.status_code, 204)
        self.assertEqual(r.headers["Access-Control-Allow-Origin"], "https://robotics.arboryx.ai")
        r = self.client.options("/admin/api/edit", headers={"Origin": "https://evil.example"})
        self.assertNotIn("Access-Control-Allow-Origin", r.headers)


if __name__ == "__main__":
    unittest.main()
