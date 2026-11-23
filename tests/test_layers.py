"""src/layers.py — role layers (mirror of graph.js computeLayerDetail) + AI healing (§2.9c part 3)."""
import types
import unittest

import duckdb

from src import layers


def db(edges, govs=()):
    """edges = [(a, rel, b)] by name; returns a connection with the minimal schema."""
    con = duckdb.connect()
    con.execute("CREATE TABLE entities (entity_id INTEGER, name VARCHAR, type VARCHAR)")
    con.execute("CREATE TABLE catalysts (catalyst_id INTEGER, headline VARCHAR, timestamp TIMESTAMP)")
    con.execute("""CREATE TABLE relationships (rel_id INTEGER, catalyst_id INTEGER, entity_a_id INTEGER, rel_type VARCHAR,
                   entity_b_id INTEGER, mechanism VARCHAR, status VARCHAR)""")
    names = sorted({n for a, _r, b in edges for n in (a, b)} | set(govs))
    ids = {n: i + 1 for i, n in enumerate(names)}
    for n, i in ids.items():
        con.execute("INSERT INTO entities VALUES (?, ?, ?)", [i, n, "government" if n in govs else "private_company"])
    con.execute("INSERT INTO catalysts VALUES (1, 'A headline', now())")
    for k, (a, rel, b) in enumerate(edges):
        con.execute("INSERT INTO relationships VALUES (?, 1, ?, ?, ?, 'because', 'active')", [k, ids[a], rel, ids[b]])
    return con, ids


class RoleLayers(unittest.TestCase):
    def test_roles_follow_relationship_direction(self):
        con, ids = db([("VC", "invests_in", "Bot"), ("Chip", "supplies", "Bot"), ("Plant", "deploys", "Bot"),
                       ("App", "built_on", "Platform"), ("Agency", "regulates", "Bot")])
        got = {n: layers.role_layers(con)[i]["layer"] for n, i in ids.items()}
        self.assertEqual(got, {"VC": "capital", "Bot": "maker", "Chip": "infra", "Plant": "deployer",
                               "App": "maker", "Platform": "infra", "Agency": "government"})

    def test_government_type_wins(self):
        con, ids = db([("Ministry", "invests_in", "Bot")], govs=("Ministry",))
        self.assertEqual(layers.role_layers(con)[ids["Ministry"]]["layer"], "government")

    def test_rival_fallback_is_a_fixed_point_and_order_free(self):
        e = [("X", "supplies", "C"), ("C", "competes_with", "A"), ("A", "competes_with", "B")]
        for order in (e, list(reversed(e))):
            con, ids = db(order)
            r = layers.role_layers(con)
            self.assertEqual([r[ids[n]]["layer"] for n in ("A", "B", "C", "X")], ["maker", "maker", "maker", "infra"])

    def test_no_role_is_other(self):
        con, ids = db([("P", "partners_with", "Q")])
        self.assertEqual(layers.role_layers(con)[ids["P"]]["layer"], "other")


class Healing(unittest.TestCase):
    def fake(self, answer):
        class Resp:
            def __init__(self, parsed):
                self.parsed, self.text = parsed, ""

        def generate_content(model, contents, config):
            ids = [int(x.split(":")[0]) for x in contents.split("### id ")[1:]]
            sch = config.response_schema
            return Resp(sch(decisions=[sch.model_fields["decisions"].annotation.__args__[0](
                entity_id=i, layer=answer, reason="from the links") for i in ids]))
        return types.SimpleNamespace(models=types.SimpleNamespace(generate_content=generate_content))

    cfg = types.SimpleNamespace(extraction=types.SimpleNamespace(model="gemini-2.5-flash", direction_thinking_level="low"))

    def test_candidates_are_no_role_and_doubtful(self):
        con, ids = db([("P", "partners_with", "Q"), ("Both", "supplies", "X"), ("Y", "invests_in", "Both"),
                       ("Both", "invests_in", "Z"), ("VC", "invests_in", "Bot")])
        c = {x["name"]: x["why"] for x in layers.candidates(con)}
        self.assertEqual(c.get("P"), "no role")
        self.assertEqual(c.get("Both"), "doubtful")          # infra 1 vs capital 1 (+ maker .5)
        self.assertNotIn("VC", c)

    def test_heal_writes_overrides_and_does_not_reask(self):
        con, ids = db([("P", "partners_with", "Q")])
        s = layers.heal(con, self.fake("capital"), self.cfg)
        self.assertEqual(s["written"], 2)
        self.assertEqual(layers.overrides(con), {"P": "capital", "Q": "capital"})
        self.assertEqual(layers.candidates(con), [])          # decided, no new links → not asked again

    def test_unknown_and_invalid_answers_are_not_overrides(self):
        con, _ids = db([("P", "partners_with", "Q")])
        layers.heal(con, self.fake("banana"), self.cfg)
        self.assertEqual(layers.overrides(con), {})
        self.assertEqual({r[0] for r in con.execute("SELECT layer FROM entity_layers").fetchall()}, {"unknown"})

    def test_dry_run_writes_nothing(self):
        con, _ids = db([("P", "partners_with", "Q")])
        s = layers.heal(con, self.fake("capital"), self.cfg, dry_run=True)
        # nothing written — not even the table (read-only callers like the cleanup checklist rely on this)
        made = con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = 'entity_layers'").fetchone()[0]
        self.assertEqual((s["candidates"], made), (2, 0))

    def test_overrides_without_table(self):
        con, _ids = db([("P", "partners_with", "Q")])
        self.assertEqual(layers.overrides(con), {})


if __name__ == "__main__":
    unittest.main()
