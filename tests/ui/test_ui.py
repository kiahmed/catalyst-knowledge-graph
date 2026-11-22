"""UI tests, step 2 (host Python + Playwright): drive the real frontend in headless Chromium.

    make test-ui                      (builds the payload from a read-only prod copy, then runs this)
    python tests/ui/test_ui.py <site_dir>

<site_dir> = a copy of frontend/ (no firebase-config.js → local mode) with cards.json from build_payload.py.
Assertions are invariants, not prod numbers, so they hold as the data grows. Firestore is SIMULATED where
noted (a fake `firebase.firestore()` behind the REAL checkUpdates) — these tests prove our page logic, not
live Firestore. Exit code 1 if anything fails.
"""
from __future__ import annotations

import http.server
import socketserver
import sys
import threading
from functools import partial

from playwright.sync_api import sync_playwright

SITE = sys.argv[1]
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):   # no per-request lines in the test output
        pass


def serve() -> tuple[socketserver.TCPServer, int]:
    srv = socketserver.TCPServer(("127.0.0.1", 0), partial(_Quiet, directory=SITE))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


DRAWN = """() => { const cy = RoboticsGraph.instance();
  return cy ? {nodes: cy.nodes().map(n => n.id()), edges: cy.edges().map(e => [e.data('source'), e.data('target'), e.data('rel')])} : null; }"""

# A fake Firestore behind the REAL RoboticsFirestore.checkUpdates: parent doc {version}, items query → docs.
FAKE_FS = """(cfg) => {
  window.FIREBASE_CONFIG = {apiKey: 'test', collection: 'CKG-Robotics', catalystsSubpath: 'catalysts'};
  window.RoboticsAuth = window.RoboticsAuth || {};
  window.__fs = {meta: {version: 'v1'}, docs: [], queries: 0};
  // where('synced_at', '>=', x) is honoured, so the real delta query (and its overlap window) is exercised
  const query = (since) => ({ get: () => { __fs.queries++; __fs.lastSince = since;
      return Promise.resolve({docs: __fs.docs.filter(d => since == null || (d.synced_at || '') >= since).map(d => ({data: () => d}))}); } });
  const items = Object.assign(query(null), { where: (f, op, v) => query(v) });
  const base = { get: () => Promise.resolve({exists: true, data: () => __fs.meta}), collection: () => items };
  window.firebase = { firestore: () => ({ collection: () => ({ doc: () => base }) }) };
  Object.assign(SYNC, {ready: true, version: 'v1', synced: '2026-11-01T00:00:00.000Z', deleted: 0, busy: false, last: 0});
}"""


def open_graph(pg, port: int, hash_: str = "view=graph") -> None:
    pg.goto(f"http://127.0.0.1:{port}/#{hash_}")
    pg.wait_for_function("() => typeof PAYLOAD !== 'undefined' && PAYLOAD && PAYLOAD.cards && PAYLOAD.cards.length > 0",
                         timeout=30000)
    pg.wait_for_timeout(2500)


def ego_entity(pg) -> str:
    """The entity with the most graph edges (stands in for 'Foxconn' — works on any data)."""
    return pg.evaluate("""() => { const c = {}; (PAYLOAD.graph.edges || []).forEach(e => { c[e.from] = (c[e.from] || 0) + 1; c[e.to] = (c[e.to] || 0) + 1; });
      return Object.entries(c).sort((a, b) => b[1] - a[1])[5][0]; }""")


def ego_ok(drawn, ego) -> bool:
    return bool(drawn) and all(ego in (s, t) for s, t, _r in drawn["edges"])


def test_ego_filter_across_windows(pg, port, sync=False):
    open_graph(pg, port)
    name = ego_entity(pg)
    if sync:
        pg.evaluate(FAKE_FS, None)
    pg.fill("#graphSearchInput", name[:6]); pg.wait_for_timeout(500)
    pg.click(f'#graphSearchSugg >> text="{name}"'); pg.wait_for_timeout(2000)   # exact match
    sizes = []
    for tw in ("90", "30", "7"):
        if sync:                                   # a backend change arrives with every lookup
            pg.evaluate("() => { __fs.meta = {version: 'v' + Date.now()}; __fs.docs = [Object.assign({}, PAYLOAD.cards[0], {synced_at: '2099-01-01T00:00:00.000Z'})]; SYNC.last = 0; }")
        pg.click(f"#gfTimeSeg .gf-seg-btn[data-tw='{tw}']"); pg.wait_for_timeout(2500)
        d = pg.evaluate(DRAWN)
        h = pg.evaluate("location.hash")
        sizes.append(len(d["edges"]) if d else -1)
        check(f"ego view keeps '{name}' at {tw}d{' (+ silent refresh with a change)' if sync else ''}",
              ego_ok(d, name) and "q=" in h, f"{len(d['nodes']) if d else 0} nodes, {sizes[-1]} edges, hash {h}")
    check(f"ego view shrinks with the window{' (+ sync)' if sync else ''}", sizes == sorted(sizes, reverse=True), str(sizes))


def test_reltype_filter(pg, port):
    open_graph(pg, port, "view=graph&tw=0")
    opts = pg.eval_on_selector_all("#gfRelType option", "os => os.map(o => o.value)")
    sub = next((o for o in opts if "/" in o), None)
    check("rel-type dropdown lists parents and subtypes", len(opts) > 2 and sub is not None, f"{len(opts)} options")
    if not sub:
        return
    pg.select_option("#gfRelType", sub); pg.wait_for_timeout(2500)
    want = pg.evaluate("(rt) => RoboticsGraph.filterPayload(PAYLOAD, currentGraphFilters()).edges.length", sub)
    d = pg.evaluate(DRAWN)
    parent = sub.split("/")[0]
    check(f"subtype filter '{sub}' draws only that type", d and all(r == parent for *_x, r in d["edges"])
          and len(d["edges"]) == want, f"{len(d['edges']) if d else 0} edges drawn, {want} expected")
    pg.select_option("#gfRelType", ""); pg.wait_for_timeout(2500)
    check("'All relationships' removes rt from the URL", "rt=" not in pg.evaluate("location.hash"))


def test_compare_and_rank(pg, port):
    """Compare box (q2): both companies' links drawn, compare panel opens; ranked list + layer summary render."""
    open_graph(pg, port, "view=graph&tw=0")
    a = ego_entity(pg)
    b = pg.evaluate("""(a) => { const c = {}; (PAYLOAD.graph.edges || []).forEach(e => { c[e.from] = (c[e.from] || 0) + 1; c[e.to] = (c[e.to] || 0) + 1; });
      return Object.entries(c).sort((x, y) => y[1] - x[1]).map(x => x[0]).find(n => n !== a && n !== 'NVIDIA'); }""", a)
    pg.evaluate("([a, b]) => { location.hash = 'view=graph&tw=0&q=' + encodeURIComponent(a) + '&q2=' + encodeURIComponent(b); onFilterChange(); }", [a, b])
    pg.wait_for_timeout(4000)
    d = pg.evaluate(DRAWN)
    check("compare draws only links touching either company", bool(d) and all(a in (s, t) or b in (s, t) for s, t, _r in d["edges"])
          and any(a in (s, t) for s, t, _r in d["edges"]) and any(b in (s, t) for s, t, _r in d["edges"]), f"{a} vs {b}")
    check("compare panel opens with both names",
          pg.evaluate("document.getElementById('nodePanel').classList.contains('open') && document.getElementById('npTitle').textContent")
          == f"{a} vs {b}")
    pg.click("#npClose"); pg.wait_for_timeout(300)
    pg.evaluate("onFilterChange()"); pg.wait_for_timeout(1500)
    check("a closed comparison stays closed on a filter change", not pg.evaluate("document.getElementById('nodePanel').classList.contains('open')"))
    rows = pg.evaluate("document.querySelectorAll('#gfRankList .gf-rank-row').length")
    check("Top in this view lists ranked companies", rows >= 5, f"{rows} rows")
    pg.click("#gfRankSeg [data-rank=layers]"); pg.wait_for_timeout(500)
    layers = pg.evaluate("document.querySelectorAll('#gfRankList .gf-rank-layer').length")
    check("layer summary shows the 5 layers", layers == 5, f"{layers}")
    pg.click("#gfColorSeg [data-color=layer]"); pg.wait_for_timeout(2500)
    check("color by layer is in the URL and the legend", "color=layer" in pg.evaluate("location.hash")
          and pg.evaluate("document.querySelector('.robotics-graph-legend').classList.contains('is-layer')"))


def test_layout(browser, port):
    for w in (1400, 1100, 900, 700):
        pg = browser.new_page(viewport={"width": w, "height": 900})
        open_graph(pg, port)
        box = lambda s: pg.eval_on_selector(s, "e => { const r = e.getBoundingClientRect(); return [r.left, r.right, r.top, r.bottom]; }")  # noqa: E731
        d, legend, search = box("#graphRelType"), box(".robotics-graph-legend"), box("#graphSearch")
        cmp_ = box("#graphCompare")
        overlap = lambda a, b: not (a[1] <= b[0] or b[1] <= a[0] or a[3] <= b[2] or b[3] <= a[2])  # noqa: E731
        check(f"rel-type dropdown overlaps nothing at {w}px", not overlap(d, legend) and not overlap(d, search))
        check(f"compare box overlaps nothing at {w}px",
              not overlap(cmp_, legend) and not overlap(cmp_, search) and not overlap(cmp_, d))
        pg.close()


def test_internal_events(pg, port):
    open_graph(pg, port)
    who = pg.evaluate("""() => { const c = (PAYLOAD.cards || []).find(c => (c.internal_events || []).length);
      return c ? c.internal_events[0].entity : null; }""")
    if not who:
        check("internal events row (no internal events in this data — skipped)", True)
        return
    n = pg.evaluate("(w) => internalEventCards(w).length", who)
    pg.evaluate("(w) => openNodePanel(w)", who); pg.wait_for_timeout(500)
    row = pg.query_selector("[data-internal-events]")
    check(f"node panel shows 'Internal events ({n})' for {who}", row is not None and f"({n})" in row.inner_text())
    if row:
        want = sorted(pg.evaluate("(w) => internalEventCards(w)", who))
        row.click(); pg.wait_for_timeout(800)
        got = sorted(pg.evaluate("filtered.map(c => c.card_id)"))
        check("clicking it shows exactly those cards (same ids)", got == want, f"{len(got)} cards")


def test_silent_refresh(pg, port):
    open_graph(pg, port, "view=cards")
    pg.evaluate("""() => { window.FIREBASE_CONFIG = {apiKey: 'test'}; window.RoboticsAuth = window.RoboticsAuth || {};
      window.__calls = 0; const real = RoboticsFirestore.checkUpdates;
      RoboticsFirestore.checkUpdates = (...a) => { __calls++; return real(...a); }; SYNC.last = 0; maybeSync(); }""")
    check("not seeded by a signed-in load (teaser/local) → no Firestore calls",
          pg.evaluate("[SYNC.ready, __calls, SYNC.busy]") == [False, 0, False])
    pg.evaluate("() => { delete window.firebase; SYNC.ready = true; SYNC.last = 0; maybeSync(); }"); pg.wait_for_timeout(500)
    check("Firebase SDK missing → real checkUpdates rejects, busy clears, page still filters",
          pg.evaluate("[SYNC.busy, filtered.length > 0]") == [False, True])
    pg.evaluate(FAKE_FS, None)
    pg.evaluate("() => { window.__before = CARDS; SYNC.last = 0; maybeSync(); }"); pg.wait_for_timeout(800)
    pg.evaluate("""() => { __fs.meta = {version: 'v1b'}; __fs.docs = [Object.assign({}, PAYLOAD.cards[2], {synced_at: '2000-01-01T00:00:00.000Z'})];
      SYNC.last = 0; maybeSync(); }"""); pg.wait_for_timeout(800)
    check("delta query skips docs older than the overlap window (real >= filter)",
          pg.evaluate("__fs.lastSince !== null && SYNC.version === 'v1b'"), pg.evaluate("String(__fs.lastSince)"))
    pg.evaluate("() => { __fs.meta = {version: 'v1b'}; __fs.docs = []; __fs.queries = 0; window.__before = CARDS; }")
    check("no backend change → 1 version read, 0 item queries, nothing re-rendered",
          pg.evaluate("[__fs.queries, CARDS === __before]") == [0, True])
    pg.evaluate("""() => { page = 2; renderPage(); const c = PAYLOAD.cards[0]; window.__gone = PAYLOAD.cards[1].card_id;
      __fs.meta = {version: 'v2', deleted_ids: [__gone + '@2099']};
      __fs.docs = [Object.assign({}, c, {headline: 'SILENTLY UPDATED', synced_at: '2099-01-01T00:00:00.000Z'})];
      SYNC.last = 0; maybeSync(); }"""); pg.wait_for_timeout(1500)
    got = pg.evaluate("""[page, SYNC.version, !!PAYLOAD.cards.find(c => c.headline === 'SILENTLY UPDATED'),
                          !!PAYLOAD.cards.find(c => c.card_id === __gone)]""")
    check("a change merges the card, drops the deleted one, keeps the Cards page", got == [2, "v2", True, False], str(got))
    pg.evaluate("() => { SYNC.last = Date.now(); window.__q = __fs.queries; __fs.meta = {version: 'v3'}; setView('graph'); }")
    pg.wait_for_timeout(800)
    check("a second lookup within 15 s makes no call (throttle)", pg.evaluate("__fs.queries === __q && SYNC.version === 'v2'"))


def main() -> int:
    srv, port = serve()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for fn, kw in ((test_ego_filter_across_windows, {}), (test_ego_filter_across_windows, {"sync": True}),
                           (test_reltype_filter, {}), (test_internal_events, {}), (test_silent_refresh, {}),
                           (test_compare_and_rank, {})):
                pg = browser.new_page(viewport={"width": 1400, "height": 900})
                errors = []
                pg.on("pageerror", lambda e, errors=errors: errors.append(str(e)))
                try:
                    fn(pg, port, **kw)
                except Exception as exc:  # noqa: BLE001 — a crashed scenario is a failure, the rest still run
                    check(f"{fn.__name__} ran", False, f"{type(exc).__name__}: {exc}")
                real = [e for e in errors if "layoutBase" not in e]   # pre-existing cytoscape layout warning
                check(f"{fn.__name__}{' sync' if kw else ''}: no page errors", not real, "; ".join(real)[:200])
                pg.close()
            test_layout(browser, port)
            browser.close()
    finally:
        srv.shutdown()
    failed = [n for n, ok, _d in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed" + (f" — FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
