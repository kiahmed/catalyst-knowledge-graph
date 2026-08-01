"""Tests for handle resolution — slug generation, verify-or-abstain matching,
cache upserts, sweep candidate selection, and card export embedding.
Run: python -m unittest tools/handle-resolver/test_handles.py -v
(needs duckdb + thefuzz + httpx — i.e. inside the ingest or resolver image).
"""
from __future__ import annotations

import os
import sys
import unittest
from dataclasses import dataclass, field

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

import duckdb  # noqa: E402

from src import db, handles  # noqa: E402


def _page(name: str) -> str:
    return f'<html><meta property="og:title" content="{name} | LinkedIn"/></html>'


_AUTHWALL = '<html><meta property="og:title" content="LinkedIn: Log In or Sign Up"/></html>'


@dataclass
class FakeResponse:
    status_code: int
    text: str = ""
    url: str = ""
    json_data: dict | None = None

    def json(self):
        return self.json_data or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"unexpected HTTP {self.status_code}")


@dataclass
class FakeClient:
    """LinkedIn pages: slug -> FakeResponse. CSE: query -> FakeResponse."""
    pages: dict[str, FakeResponse] = field(default_factory=dict)
    cse: dict[str, FakeResponse] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def post(self, url: str, json: dict | None = None, headers: dict | None = None) -> FakeResponse:
        q = (json or {}).get("q", "")
        self.calls.append(f"serper:{q}")
        return self.cse.get(q, FakeResponse(200, json_data={"organic": []}))

    def get(self, url: str, params: dict | None = None) -> FakeResponse:
        if "customsearch" in url:
            q = (params or {}).get("q", "")
            self.calls.append(f"cse:{q}")
            return self.cse.get(q, FakeResponse(200, json_data={"items": []}))
        slug = url.rsplit("/", 1)[-1]
        self.calls.append(slug)
        resp = self.pages.get(slug, FakeResponse(404))
        resp.url = resp.url or url
        return resp


def _cse_items(*pairs: tuple[str, str]) -> FakeResponse:
    return FakeResponse(
        200, json_data={"items": [{"title": t, "link": l} for (t, l) in pairs]}
    )


CSE = handles.CseConfig("test-key", "test-cx")
SERPER = handles.SerperConfig("test-key")
SEARCHAPI = handles.SearchApiConfig("test-key")


class TestSlugCandidates(unittest.TestCase):
    def test_basic_and_suffix_stripped(self):
        cands = handles.slug_candidates("Figure AI")
        self.assertIn("figure-ai", cands)
        self.assertIn("figure", cands)      # 'ai' is a generic token
        self.assertIn("figureai", cands)

    def test_aliases_contribute(self):
        cands = handles.slug_candidates("Schaeffler Group", ["Schaeffler"])
        self.assertEqual(cands[0], "schaeffler-group")
        self.assertIn("schaeffler", cands)

    def test_punctuation_and_ampersand(self):
        self.assertIn("johnson-and-johnson", handles.slug_candidates("Johnson & Johnson"))

    def test_never_strips_to_empty(self):
        self.assertEqual(handles.slug_candidates("AI")[0], "ai")


class TestVerification(unittest.TestCase):
    def test_page_name_parse(self):
        self.assertEqual(handles._page_company_name(_page("Figure")), "Figure")
        self.assertIsNone(handles._page_company_name(_AUTHWALL))
        self.assertIsNone(handles._page_company_name("<html>no og title</html>"))

    def test_match_accepts_suffix_variants(self):
        self.assertGreaterEqual(handles._match_score("Figure", ["Figure AI"]), 90)
        self.assertGreaterEqual(
            handles._match_score("Schaeffler Group", ["Schaeffler Group"]), 90
        )

    def test_slug_rejects_unpostable_forms(self):
        self.assertIsNone(handles._slug_from_url(
            "https://www.linkedin.com/company/%E4%B8%8A%E6%B5%B7%E9%BE%99"))
        self.assertIsNone(handles._slug_from_url(
            "https://www.linkedin.com/company/a&k-robotics"))
        self.assertEqual(handles._slug_from_url(
            "https://www.linkedin.com/company/figure-ai/about"), "figure-ai")

    def test_match_folds_diacritics_and_energy_suffix(self):
        # "zenobē" vs profile "Zenobe Energy" — must match (was score 56)
        self.assertGreaterEqual(handles._match_score("Zenobe Energy", ["Zenobē"]), 90)

    def test_match_rejects_different_company(self):
        # Common-word entity vs an unrelated real page — must abstain.
        self.assertLess(handles._match_score("Humanoid Global Holdings", ["Humanoid"]), 90)
        self.assertLess(handles._match_score("Figure Skating Club", ["Figure AI"]), 90)


class TestResolveLinkedIn(unittest.TestCase):
    def test_verified(self):
        client = FakeClient({"figure-ai": FakeResponse(200, _page("Figure"))})
        r = handles.resolve_linkedin("Figure AI", client=client, delay_s=0)
        self.assertEqual(r.handle, "@figure-ai")
        self.assertEqual(r.source, handles.SOURCE_LINKEDIN)
        self.assertGreaterEqual(r.confidence, 0.9)

    def test_uses_redirected_slug(self):
        client = FakeClient({
            "figure-ai": FakeResponse(
                200, _page("Figure"), url="https://www.linkedin.com/company/figureai"
            )
        })
        r = handles.resolve_linkedin("Figure AI", client=client, delay_s=0)
        self.assertEqual(r.handle, "@figureai")

    def test_abstains_on_wrong_company(self):
        client = FakeClient({"humanoid": FakeResponse(200, _page("Humanoid Global Holdings"))})
        r = handles.resolve_linkedin("Humanoid", client=client, delay_s=0)
        self.assertIsNone(r.handle)
        self.assertEqual(r.source, handles.SOURCE_ABSTAIN)

    def test_abstains_on_404s(self):
        r = handles.resolve_linkedin("Nonexistent Corp", client=FakeClient(), delay_s=0)
        self.assertIsNone(r.handle)
        self.assertEqual(r.source, handles.SOURCE_ABSTAIN)

    def test_blocked_on_authwall(self):
        client = FakeClient({
            s: FakeResponse(200, _AUTHWALL)
            for s in handles.slug_candidates("Figure AI")
        })
        r = handles.resolve_linkedin("Figure AI", client=client, delay_s=0)
        self.assertIsNone(r.handle)
        self.assertEqual(r.source, handles.SOURCE_BLOCKED)

    def test_x_always_abstains(self):
        r = handles.resolve_channel("x", "Figure AI")
        self.assertIsNone(r.handle)


class TestSerpResolution(unittest.TestCase):
    def setUp(self):
        self._delay = handles.DIRECT_FALLBACK_DELAY_S
        handles.DIRECT_FALLBACK_DELAY_S = 0

    def tearDown(self):
        handles.DIRECT_FALLBACK_DELAY_S = self._delay

    def test_linkedin_serp_verified(self):
        client = FakeClient(cse={
            'site:linkedin.com/company "Figure AI"': _cse_items(
                ("Figure | LinkedIn", "https://www.linkedin.com/company/figure-ai"),
            )
        })
        r = handles.resolve_channel("linkedin", "Figure AI", client=client, search=CSE)
        self.assertEqual((r.handle, r.source), ("@figure-ai", handles.SOURCE_LINKEDIN_SERP))

    def test_linkedin_serp_abstains_on_mismatch(self):
        client = FakeClient(cse={
            'site:linkedin.com/company "Humanoid"': _cse_items(
                ("Humanoid Global Holdings | LinkedIn",
                 "https://www.linkedin.com/company/humanoid-global"),
            )
        })
        r = handles.resolve_channel("linkedin", "Humanoid", client=client, search=CSE)
        self.assertIsNone(r.handle)
        self.assertEqual(r.source, handles.SOURCE_ABSTAIN)
        self.assertIn("humanoid-global", r.comment)

    def test_linkedin_empty_serp_falls_back_to_direct(self):
        client = FakeClient(
            cse={},    # no results for any query
            pages={"figure-ai": FakeResponse(200, _page("Figure"))},
        )
        r = handles.resolve_channel("linkedin", "Figure AI", client=client, search=CSE)
        self.assertEqual((r.handle, r.source), ("@figure-ai", handles.SOURCE_LINKEDIN))

    def test_x_serp_verified(self):
        client = FakeClient(cse={
            'site:x.com "Figure AI"': _cse_items(
                ("Figure (@Figure_robot) / X", "https://x.com/Figure_robot"),
            )
        })
        r = handles.resolve_channel("x", "Figure AI", client=client, search=CSE)
        self.assertEqual((r.handle, r.source), ("@Figure_robot", handles.SOURCE_X_SERP))

    def test_x_serp_posts_suffix_title(self):
        # Google's current title shape: "ABB (@ABB) / Posts / X - Twitter"
        client = FakeClient(cse={
            'site:x.com "ABB"': _cse_items(
                ("ABB (@ABB) / Posts / X - Twitter", "https://x.com/ABB"),
            )
        })
        r = handles.resolve_channel("x", "ABB", client=client, search=CSE)
        self.assertEqual(r.handle, "@ABB")

    def test_x_serp_rejects_fan_account_name(self):
        client = FakeClient(cse={
            'site:x.com "KUKA"': _cse_items(
                ("KUKA Fan (@KUKA_Robotics) / Posts / X - Twitter",
                 "https://x.com/KUKA_Robotics"),
            )
        })
        r = handles.resolve_channel("x", "KUKA", client=client, search=CSE)
        self.assertIsNone(r.handle)
        self.assertIn("KUKA_Robotics", r.comment)

    def test_x_serp_skips_tweets_and_mismatches(self):
        client = FakeClient(cse={
            'site:x.com "Humanoid"': _cse_items(
                # a tweet, not a profile — no (@handle) / X title
                ("Brett Adcock on X: robots are coming", "https://x.com/adcock/status/1"),
                # a profile whose display name doesn't match
                ("Humanoid Global (@HumanoidGlobal) / X", "https://x.com/HumanoidGlobal"),
            )
        })
        r = handles.resolve_channel("x", "Humanoid", client=client, search=CSE)
        self.assertIsNone(r.handle)
        self.assertEqual(r.source, handles.SOURCE_ABSTAIN)

    def test_cse_quota_maps_to_blocked(self):
        client = FakeClient(cse={
            'site:linkedin.com/company "Figure AI"': FakeResponse(429, text="rateLimitExceeded"),
            'site:x.com "Figure AI"': FakeResponse(403, text="dailyLimitExceeded"),
        })
        for channel in ("linkedin", "x"):
            r = handles.resolve_channel(channel, "Figure AI", client=client, search=CSE)
            self.assertEqual(r.source, handles.SOURCE_BLOCKED)

    def test_linkedin_ambiguous_slugs_abstain(self):
        # Two DIFFERENT slugs both matching the name → abstain, don't guess.
        client = FakeClient(cse={
            'site:linkedin.com/company "FANUC"': _cse_items(
                ("FANUC | LinkedIn", "https://www.linkedin.com/company/fanuc"),
                ("FANUC America Corporation | LinkedIn",
                 "https://www.linkedin.com/company/fanuc-america-corporation"),
            )
        })
        r = handles.resolve_channel(
            "linkedin", "FANUC", ["FANUC America"], client=client, search=CSE)
        self.assertIsNone(r.handle)
        self.assertIn("ambiguous", r.comment)

    def test_linkedin_same_slug_twice_still_verifies(self):
        # Same page appearing twice (e.g. /about subpage) is NOT ambiguity.
        client = FakeClient(cse={
            'site:linkedin.com/company "Figure AI"': _cse_items(
                ("Figure | LinkedIn", "https://www.linkedin.com/company/figure-ai"),
                ("Figure | LinkedIn", "https://www.linkedin.com/company/figure-ai/about"),
            )
        })
        r = handles.resolve_channel("linkedin", "Figure AI", client=client, search=CSE)
        self.assertEqual(r.handle, "@figure-ai")

    def test_x_ambiguous_profiles_abstain_and_casing_kept(self):
        client = FakeClient(cse={
            'site:x.com "Acme"': _cse_items(
                ("Acme (@AcmeCorp) / X", "https://x.com/AcmeCorp"),
                ("Acme (@Acme_Official) / X", "https://x.com/Acme_Official"),
            ),
            'site:x.com "Figure AI"': _cse_items(
                ("Figure (@Figure_robot) / X", "https://x.com/Figure_robot"),
            ),
        })
        r = handles.resolve_channel("x", "Acme", client=client, search=CSE)
        self.assertIsNone(r.handle)
        self.assertIn("ambiguous", r.comment)
        r = handles.resolve_channel("x", "Figure AI", client=client, search=CSE)
        self.assertEqual(r.handle, "@Figure_robot")   # original casing preserved

    def test_context_breaks_name_tie(self):
        # Two same-named companies; the snippet mentioning robotics/partners
        # wins because of the entity's card context.
        ctx = handles.context_terms_from(
            "Robotics robot robots", ["Figure", "Unitree"],
            ["Spirit AI unveils humanoid robot platform"])
        client = FakeClient(cse={
            'site:linkedin.com/company "Spirit AI"': FakeResponse(200, json_data={"items": [
                {"title": "Spirit AI | LinkedIn",
                 "link": "https://www.linkedin.com/company/spirit-ai",
                 "snippet": "Conversational game dialogue and character AI tools."},
                {"title": "Spirit AI | LinkedIn",
                 "link": "https://www.linkedin.com/company/spiritai-robotics",
                 "snippet": "We build general-purpose humanoid robots."},
            ]})
        })
        r = handles.resolve_channel(
            "linkedin", "Spirit AI", client=client, search=CSE, context=ctx)
        self.assertEqual(r.handle, "@spiritai-robotics")
        self.assertIn("won on context", r.comment)

    def test_context_tie_still_abstains(self):
        ctx = handles.context_terms_from("Robotics", [], [])
        client = FakeClient(cse={
            'site:linkedin.com/company "Acme"': FakeResponse(200, json_data={"items": [
                {"title": "Acme | LinkedIn",
                 "link": "https://www.linkedin.com/company/acme",
                 "snippet": "Industrial robotics."},
                {"title": "Acme | LinkedIn",
                 "link": "https://www.linkedin.com/company/acme-global",
                 "snippet": "Robotics and automation."},
            ]})
        })
        r = handles.resolve_channel("linkedin", "Acme", client=client, search=CSE, context=ctx)
        self.assertIsNone(r.handle)   # both hit 'robotics' — no strict winner
        self.assertIn("ambiguous", r.comment)

    def test_single_match_zero_context_flags_review(self):
        ctx = handles.context_terms_from("Robotics robot robots", ["Figure"], [])
        client = FakeClient(cse={
            'site:linkedin.com/company "Spirit AI"': FakeResponse(200, json_data={"items": [
                {"title": "Spirit AI | LinkedIn",
                 "link": "https://www.linkedin.com/company/spirit-ai",
                 "snippet": "Conversational game dialogue tools."},
            ]})
        })
        r = handles.resolve_channel(
            "linkedin", "Spirit AI", client=client, search=CSE, context=ctx)
        self.assertEqual(r.handle, "@spirit-ai")     # still stored...
        self.assertEqual(r.confidence, 0.75)          # ...but flagged
        self.assertIn("industry unconfirmed", r.comment)

    def test_serper_provider_linkedin_and_x(self):
        client = FakeClient(cse={
            'site:linkedin.com/company "Figure AI"': FakeResponse(
                200, json_data={"organic": [
                    {"title": "Figure | LinkedIn",
                     "link": "https://www.linkedin.com/company/figure-ai"},
                ]}
            ),
            'site:x.com "Figure AI"': FakeResponse(
                200, json_data={"organic": [
                    {"title": "Figure (@Figure_robot) / X",
                     "link": "https://x.com/Figure_robot"},
                ]}
            ),
        })
        r = handles.resolve_channel("linkedin", "Figure AI", client=client, search=SERPER)
        self.assertEqual((r.handle, r.source), ("@figure-ai", handles.SOURCE_LINKEDIN_SERP))
        r = handles.resolve_channel("x", "Figure AI", client=client, search=SERPER)
        self.assertEqual((r.handle, r.source), ("@Figure_robot", handles.SOURCE_X_SERP))

    def test_brave_provider(self):
        brave = handles.BraveConfig("test-key")
        client = FakeClient()
        real_get = client.get
        def get(url, params=None, headers=None):
            if "api.search.brave.com" in url:
                q = (params or {}).get("q", "")
                client.calls.append(f"brave:{q}")
                if q == 'site:linkedin.com/company "Figure AI"':
                    return FakeResponse(200, json_data={"web": {"results": [
                        {"title": "Figure | LinkedIn",
                         "url": "https://www.linkedin.com/company/figure-ai",
                         "description": "General purpose humanoid robots."},
                    ]}})
                return FakeResponse(200, json_data={"web": {"results": []}})
            return real_get(url, params)
        client.get = get
        r = handles.resolve_channel("linkedin", "Figure AI", client=client, search=brave)
        self.assertEqual((r.handle, r.source), ("@figure-ai", handles.SOURCE_LINKEDIN_SERP))

    def test_serper_quota_maps_to_blocked(self):
        client = FakeClient(cse={
            'site:linkedin.com/company "Figure AI"': FakeResponse(403, text="quota"),
        })
        r = handles.resolve_channel("linkedin", "Figure AI", client=client, search=SERPER)
        self.assertEqual(r.source, handles.SOURCE_BLOCKED)

    def test_searchapi_provider(self):
        client = FakeClient(cse={
            'site:linkedin.com/company "Figure AI"': FakeResponse(
                200, json_data={"organic_results": [
                    {"title": "Figure | LinkedIn",
                     "link": "https://www.linkedin.com/company/figure-ai"},
                ]}
            ),
        })
        # SearchApi uses GET on searchapi.io — route it in the fake
        real_get = client.get
        def get(url, params=None, headers=None):
            if "searchapi.io" in url:
                q = (params or {}).get("q", "")
                client.calls.append(f"searchapi:{q}")
                return client.cse.get(q, FakeResponse(200, json_data={"organic_results": []}))
            return real_get(url, params)
        client.get = get
        r = handles.resolve_channel("linkedin", "Figure AI", client=client, search=SEARCHAPI)
        self.assertEqual((r.handle, r.source), ("@figure-ai", handles.SOURCE_LINKEDIN_SERP))

    def test_x_without_cse_stays_manual(self):
        r = handles.resolve_channel("x", "Figure AI", client=FakeClient(), search=None)
        self.assertIsNone(r.handle)
        self.assertEqual(r.source, handles.SOURCE_ABSTAIN)


class TestCacheTable(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect(":memory:")
        db.init_schema(self.con)

    def tearDown(self):
        self.con.close()

    def test_upsert_and_get(self):
        db.upsert_handle(self.con, "figure ai", "linkedin", "@figure-ai", 1.0,
                         handles.SOURCE_LINKEDIN)
        db.upsert_handle(self.con, "figure ai", "x", None, 0.0, handles.SOURCE_ABSTAIN)
        got = db.get_handles(self.con, ["figure ai"])
        self.assertEqual(got[("figure ai", "linkedin")],
                         ("@figure-ai", handles.SOURCE_LINKEDIN, 1.0))
        self.assertEqual(got[("figure ai", "x")], (None, handles.SOURCE_ABSTAIN, 0.0))

    def test_upsert_overwrites(self):
        db.upsert_handle(self.con, "k", "linkedin", None, 0.0, handles.SOURCE_BLOCKED)
        db.upsert_handle(self.con, "k", "linkedin", "@k", 0.95, handles.SOURCE_LINKEDIN)
        got = db.get_handles(self.con, ["k"])
        self.assertEqual(got[("k", "linkedin")][0], "@k")

    def test_comment_stored_and_reported(self):
        db.insert_entity(self.con, "Figure AI", None, "private_company")
        db.insert_entity(self.con, "Kuka", None, "public_company")
        db.upsert_handle(self.con, "kuka", "linkedin", None, 0.0,
                         handles.SOURCE_ABSTAIN, "no candidate verified; closest score 70")
        report = db.unresolved_handles_report(self.con)
        self.assertEqual(len(report["unresolved"]), 1)
        row = report["unresolved"][0]
        self.assertEqual((row["entity"], row["channel"]), ("kuka", "linkedin"))
        self.assertIn("closest score 70", row["comment"])
        self.assertEqual(report["never_attempted"], ["Figure AI"])

    def test_missing_handles_selection(self):
        db.insert_entity(self.con, "Figure AI", None, "private_company")
        db.insert_entity(self.con, "Jane Doe", None, "person")
        db.insert_entity(self.con, "Schaeffler Group", "SHA", "public_company")
        # Figure AI already decided; Schaeffler blocked (retryable after cooldown).
        db.upsert_handle(self.con, "figure ai", "linkedin", "@figure-ai", 1.0,
                         handles.SOURCE_LINKEDIN)
        db.upsert_handle(self.con, "schaeffler group", "linkedin", None, 0.0,
                         handles.SOURCE_BLOCKED)
        # Fresh blocked row is inside the 14-day cooldown — NOT retried yet.
        todo = db.entities_missing_handles(self.con, "linkedin", 10)
        self.assertEqual([n for n, _, _ in todo], [])
        # After the cooldown ages out, it becomes retryable (flagged straggler).
        self.con.execute(
            "UPDATE entity_handles SET resolved_at = now() - INTERVAL 15 DAY"
            " WHERE name_key = 'schaeffler group'")
        todo = db.entities_missing_handles(self.con, "linkedin", 10)
        self.assertEqual([(n, b) for n, _, b in todo],
                         [("Schaeffler Group", True)])

    def test_straggler_probe_stops_after_no_wins(self):
        from src import handle_sweep as hs
        import os
        # 8 aged-out blocked entities; SERPs empty, direct fetch walled →
        # first probe re-blocks; after 5 tries with 0 wins the rest defer.
        for i in range(8):
            db.insert_entity(self.con, f"Ghost Corp {i}", None, "private_company")
            db.upsert_handle(self.con, f"ghost corp {i}", "linkedin", None, 0.0,
                             handles.SOURCE_BLOCKED)
            db.upsert_handle(self.con, f"ghost corp {i}", "x", None, 0.0,
                             handles.SOURCE_ABSTAIN)
        self.con.execute(
            "UPDATE entity_handles SET resolved_at = now() - INTERVAL 15 DAY")
        client = FakeClient(cse={},
                            pages={s: FakeResponse(999)
                                   for i in range(8)
                                   for s in handles.slug_candidates(f"Ghost Corp {i}")})
        os.environ["BRAVE_API_KEY"] = "test-key"
        real_get = client.get
        def get(url, params=None, headers=None):
            if "api.search.brave.com" in url:
                return FakeResponse(200, json_data={"web": {"results": []}})
            return real_get(url, params)
        client.get = get
        old_delay = handles.SEARCH_DELAY_S
        old_direct = handles.DIRECT_FALLBACK_DELAY_S
        handles.SEARCH_DELAY_S = 0
        handles.DIRECT_FALLBACK_DELAY_S = 0
        try:
            counts = hs.sweep(self.con, client, limit=10)
        finally:
            handles.SEARCH_DELAY_S = old_delay
            handles.DIRECT_FALLBACK_DELAY_S = old_direct
            del os.environ["BRAVE_API_KEY"]
        self.assertEqual(counts["resolved"], 0)
        self.assertGreaterEqual(counts.get("stragglers_deferred", 0), 3)
        self.assertLessEqual(counts["blocked"], 5)


class TestReauditSelection(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect(":memory:")
        db.init_schema(self.con)
        db.upsert_handle(self.con, "figure ai", "linkedin", "@figure-ai", 1.0,
                         handles.SOURCE_LINKEDIN_SERP)
        db.upsert_handle(self.con, "spirit ai", "linkedin", "@spirit-ai", 0.75,
                         handles.SOURCE_LINKEDIN_SERP, "industry unconfirmed")
        db.upsert_handle(self.con, "kuka", "x", None, 0.0, handles.SOURCE_ABSTAIN)
        db.upsert_handle(self.con, "manual co", "x", "@manual", 1.0, handles.SOURCE_HUMAN)
        db.upsert_handle(self.con, "walled co", "linkedin", None, 0.0, handles.SOURCE_BLOCKED)

    def tearDown(self):
        self.con.close()

    def test_confidence_filter_and_exclusions(self):
        rows = db.handles_for_reaudit(self.con, ["linkedin", "x"], 0.9, None, 100)
        keys = {(k, c) for (k, c, *_ ) in rows}
        self.assertIn(("spirit ai", "linkedin"), keys)   # low confidence
        self.assertIn(("kuka", "x"), keys)               # abstain (0.0)
        self.assertNotIn(("figure ai", "linkedin"), keys)  # conf 1.0
        self.assertNotIn(("manual co", "x"), keys)       # human — never
        self.assertNotIn(("walled co", "linkedin"), keys)  # blocked — sweep's job

    def test_names_override_confidence(self):
        rows = db.handles_for_reaudit(
            self.con, ["linkedin", "x"], 0.9, ["figure ai"], 100)
        self.assertEqual([(r[0], r[1]) for r in rows], [("figure ai", "linkedin")])

    def test_resume_skips_audited(self):
        db.mark_handle_audited(self.con, "spirit ai", "linkedin")
        rows = db.handles_for_reaudit(self.con, ["linkedin"], 0.9, None, 100)
        self.assertEqual(rows, [])
        rows = db.handles_for_reaudit(self.con, ["linkedin"], 0.9, None, 100, force=True)
        self.assertEqual(len(rows), 1)


class TestSearchBudget(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect(":memory:")
        db.init_schema(self.con)   # seeds provider rows

    def tearDown(self):
        self.con.close()

    def test_seeded_and_ordered(self):
        rows = db.get_search_providers(self.con)
        self.assertEqual([r["provider"] for r in rows],
                         ["brave", "serper", "searchapi", "cse"])
        brave = rows[0]
        self.assertTrue(brave["enabled"])
        self.assertEqual(brave["monthly_quota"], 1000)
        self.assertEqual(brave["refill_day"], 11)

    def test_seed_never_overwrites_edits(self):
        self.con.execute(
            "UPDATE search_providers SET priority = 9 WHERE provider = 'brave'")
        db.seed_search_providers(self.con)
        rows = {r["provider"]: r for r in db.get_search_providers(self.con)}
        self.assertEqual(rows["brave"]["priority"], 9)

    def test_bump_and_reset(self):
        db.bump_search_usage(self.con, "brave", 3)
        rows = {r["provider"]: r for r in db.get_search_providers(self.con)}
        self.assertEqual(rows["brave"]["used_this_cycle"], 3)
        from datetime import date
        db.reset_search_cycle(self.con, "brave", date(2026, 8, 11))
        rows = {r["provider"]: r for r in db.get_search_providers(self.con)}
        self.assertEqual(rows["brave"]["used_this_cycle"], 0)

    def test_usage_hook_fires_on_search(self):
        counted = []
        handles.search_usage_hook = lambda p: counted.append(p)
        try:
            client = FakeClient()
            handles._web_search(client, SERPER, "anything")
        finally:
            handles.search_usage_hook = None
        self.assertEqual(counted, ["serper"])


class TestExportEmbedding(unittest.TestCase):
    def test_cards_carry_handles(self):
        from src.export import _load_cards

        con = duckdb.connect(":memory:")
        db.init_schema(con)
        eid_a = db.insert_entity(con, "Figure AI", None, "private_company")
        eid_b = db.insert_entity(con, "Schaeffler Group", "SHA", "public_company")
        con.execute(
            """INSERT INTO catalysts (entry_id, sector, timestamp, raw_finding,
               headline, sentiment_label) VALUES
               ('ROB-010126-001', 'Robotics', CURRENT_DATE, 'raw', 'Headline', 'bullish')"""
        )
        cid = con.execute("SELECT catalyst_id FROM catalysts").fetchone()[0]
        con.execute(
            """INSERT INTO relationships (catalyst_id, entity_a_id, rel_type,
               entity_b_id, confidence) VALUES (?, ?, 'supplies', ?, 0.9)""",
            [cid, eid_b, eid_a],
        )
        db.upsert_handle(con, "figure ai", "linkedin", "@figure-ai", 1.0,
                         handles.SOURCE_LINKEDIN)
        # Review-flagged (below MIN_TAG_CONFIDENCE) — must stay OFF the card.
        db.upsert_handle(con, "schaeffler group", "linkedin", "@apollo-gold", 0.75,
                         handles.SOURCE_LINKEDIN_SERP, "industry unconfirmed — review")

        cards = _load_cards(con, "Robotics", 10, 0, 0.0, False)
        self.assertEqual(len(cards), 1)
        by_name = {e["name"]: e for e in cards[0]["entities"]}
        self.assertEqual(by_name["Figure AI"]["linkedin_handle"], "@figure-ai")
        self.assertIsNone(by_name["Figure AI"]["x_handle"])
        self.assertIsNone(by_name["Schaeffler Group"]["linkedin_handle"])
        con.close()


if __name__ == "__main__":
    unittest.main()
