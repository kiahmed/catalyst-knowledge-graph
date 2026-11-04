"""Tests for src/embed.py + src/embed_sync.py (fake clients, no live calls).

Run: python -m unittest tests/test_embed.py -v   (inside the ingest image)
"""
from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace as N

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import duckdb  # noqa: E402

from src import db, embed, embed_sync, entities  # noqa: E402


class FakeEmbedClient:
    def __init__(self):
        self.models, self.calls = self, []

    def embed_content(self, model, contents, config):
        self.calls.append((model, len(contents), config.output_dimensionality))
        return N(embeddings=[N(values=[float(i)] * config.output_dimensionality) for i in range(len(contents))])


class FakeDoc:
    def __init__(self, store, did):
        self.store, self.id = store, did

    def to_dict(self):
        return self.store.get(self.id)


class FakeItems:
    def __init__(self, store):
        self.store = store

    def select(self, _fields):
        return self

    def stream(self):
        return [FakeDoc(self.store, k) for k in list(self.store)]

    def document(self, did):
        return did


class FakeFS:
    def __init__(self, store):
        self.store = store

    def batch(self):
        fs = self

        class B:
            def set(self, did, data, merge=False):
                fs.store.setdefault(did, {}).update(data)

            def commit(self):
                pass
        return B()


class TestEmbed(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect(":memory:")
        db.init_schema(self.con)
        self.con.execute("""INSERT INTO catalysts (entry_id, sector, timestamp, raw_finding, headline, sentiment_label)
                            VALUES ('ROB-1', 'Robotics', DATE '2026-10-01', 'Atlas units work at the Metaplant.',
                                    'Hyundai runs Atlas', 'bullish')""")
        cid = self.con.execute("SELECT catalyst_id FROM catalysts").fetchone()[0]
        self.hmg = db.insert_entity(self.con, "Hyundai Motor Group", None, "public_company")
        self.bd = db.insert_entity(self.con, "Boston Dynamics", None, "private_company")
        self.con.execute("""INSERT INTO relationships (catalyst_id, entity_a_id, rel_type, entity_b_id, confidence, mechanism)
                            VALUES (?, ?, 'deploys', ?, 0.9, 'Atlas runs sequencing tasks.')""", [cid, self.hmg, self.bd])
        entities.link(self.con, self.bd, self.hmg, "subsidiary_of", confidence=1, reason="r", source="test")

    def tearDown(self):
        self.con.close()

    def test_text_carries_direction_mechanism_and_family(self):
        inp = embed.card_inputs(self.con, "Robotics")["ROB-1"]
        self.assertTrue(inp["text"].startswith("title: Hyundai runs Atlas | text: Atlas units work"))
        self.assertIn("Hyundai Motor Group deploys (as a customer) products of Boston Dynamics. Atlas runs", inp["text"])
        self.assertIn("Boston Dynamics is a subsidiary of Hyundai Motor Group.", inp["text"])
        self.assertEqual((inp["entity_names"], inp["rel_types"]), (["Boston Dynamics", "Hyundai Motor Group"], ["deploys"]))
        self.assertEqual(inp["hash"], embed.card_inputs(self.con, "Robotics")["ROB-1"]["hash"])   # stable

    def _sync(self, cards, vecs, client, **kw):
        return embed_sync.sync(self.con, "Robotics", FakeFS(vecs), FakeItems(cards), FakeItems(vecs), client, **kw)

    def test_sync_embeds_only_stale_cards_into_the_vector_collection(self):
        cards, vecs = {"ROB-1": {"headline": "h"}}, {}    # card doc exists, no vector yet
        client = FakeEmbedClient()
        self.assertEqual(self._sync(cards, vecs, client)["embedded"], 1)
        self.assertEqual(client.calls, [(embed.MODEL, 1, embed.DIMENSIONS)])
        self.assertEqual(cards, {"ROB-1": {"headline": "h"}})                        # card doc never touched
        v = vecs["ROB-1"]
        self.assertEqual(len(v["embedding"]), embed.DIMENSIONS)
        self.assertEqual((v["card_id"], v["headline"], v["date"]), ("ROB-1", "Hyundai runs Atlas", "2026-10-01"))
        self.assertEqual(v["entity_names"], ["Boston Dynamics", "Hyundai Motor Group"])
        self.assertEqual(self._sync(cards, vecs, client)["to_embed"], 0)
        self.con.execute("UPDATE relationships SET mechanism = 'changed'")          # text changed → stale again
        old_hash = v["embed_hash"]
        self.assertEqual(self._sync(cards, vecs, client)["embedded"], 1)
        self.assertNotEqual(vecs["ROB-1"]["embed_hash"], old_hash)                   # full round trip

    def test_prune_drops_vectors_of_deleted_cards(self):
        cards, vecs = {"ROB-1": {}}, {"ROB-1": {"embed_hash": "x"}, "ROB-GONE": {"embed_hash": "y"}}

        class DelFS(FakeFS):
            def batch(self):
                b = super().batch()
                b.delete = lambda did: vecs.pop(did, None)
                return b
        self.assertEqual(embed_sync.prune(FakeItems(cards), FakeItems(vecs), DelFS(vecs)), 1)
        self.assertEqual(set(vecs), {"ROB-1"})

    def test_cards_without_a_firestore_doc_are_skipped(self):
        cards, vecs, client = {}, {}, FakeEmbedClient()
        out = self._sync(cards, vecs, client)
        self.assertEqual((out["to_embed"], vecs, client.calls), (0, {}, []))        # no call, no write

    def test_wrong_size_vectors_are_rejected(self):
        class Short(FakeEmbedClient):
            def embed_content(self, model, contents, config):
                return N(embeddings=[N(values=[0.1] * 10) for _ in contents])
        with self.assertRaises(RuntimeError):
            embed.embed_texts(Short(), ["a"])

    def test_text_is_order_stable_and_keeps_every_family_line(self):
        kia = db.insert_entity(self.con, "Kia", None, "public_company")
        entities.link(self.con, self.bd, kia, "sister_of", confidence=1, reason="r", source="test")
        text = embed.card_inputs(self.con, "Robotics")["ROB-1"]["text"]
        self.assertIn("Boston Dynamics is a subsidiary of Hyundai Motor Group.", text)      # parent AND
        self.assertIn("Boston Dynamics and Kia share an owner.", text)                       # sister both kept

    def test_query_prefix(self):
        self.assertEqual(embed.query_text("NVIDIA deals"), "task: search result | query: NVIDIA deals")


if __name__ == "__main__":
    unittest.main()
