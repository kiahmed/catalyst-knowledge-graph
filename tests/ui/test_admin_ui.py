"""Admin UI tests (host Python + Playwright) against tests/ui/admin_server.py — the real admin app in test mode on a
LOCAL copy of a prod snapshot (nothing reaches prod). Each scenario makes its own state through the UI and checks
the result through the admin API. Exit code 1 if anything fails.

    python tests/ui/test_admin_ui.py <base_url>        (make test-ui starts the server and runs this)
"""
from __future__ import annotations

import json
import sys
import urllib.request

from playwright.sync_api import sync_playwright

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8791").rstrip("/")
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))


def api(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE}/admin/api{path}", timeout=60) as r:
        return json.load(r)


def rel(eid: int, rel_id: int) -> dict | None:
    return next((r for r in api(f"/entity/{eid}")["relationships"] if r["rel_id"] == rel_id), None)


class Dialogs:
    """Answer confirm/prompt dialogs in order; anything unexpected is accepted (and recorded)."""

    def __init__(self, page):
        self.queue: list = []
        self.seen: list[str] = []
        page.on("dialog", self._on)

    def _on(self, d):
        self.seen.append(f"{d.type}: {d.message[:80]}")
        ans = self.queue.pop(0) if self.queue else True
        if ans is False:
            d.dismiss()
        elif isinstance(ans, str):
            d.accept(ans)
        else:
            d.accept()


def open_entity(pg, name: str) -> None:
    pg.fill("#q", name)
    pg.wait_for_timeout(900)
    # Wait for THIS entity's load to finish: re-opening the same entity re-renders the lists, and a click made
    # before that lands on the old list (the reload then clears the selection).
    with pg.expect_response(lambda r: "/admin/api/entity/" in r.url, timeout=30000):
        pg.locator("#entityList .item", has_text=name).first.click()
    pg.wait_for_selector("#relList .item", timeout=20000)
    pg.wait_for_timeout(400)


def pick_entity(min_live: int = 3) -> dict:
    """A real company with a few live, between-entity edges (from the snapshot)."""
    for e in api("/entities?limit=200&q=")["entities"]:
        d = api(f"/entity/{e['id']}")
        live = [r for r in d["relationships"] if r["status"] == "active"]
        if d["type"] != "group" and len(live) >= min_live:
            return d
    raise RuntimeError("no entity with enough live edges in the snapshot")


def first_live(d: dict) -> dict:
    return next(r for r in d["relationships"] if r["status"] == "active")


def test_boot(pg, dlg):
    pg.goto(f"{BASE}/admin.html")
    pg.wait_for_selector("#entityList .item", timeout=30000)
    check("admin page boots signed in (fake user) and lists entities", "ui-test@local" in pg.inner_text("#who"))


def test_reject_then_restore(pg, dlg):
    d = pick_entity()
    r = first_live(d)
    open_entity(pg, d["name"])
    pg.locator("#relList .item", has_text=f"{r['a']['name']} —{r['rel_type']}→ {r['b']['name']}").first.click()
    dlg.queue.append(True)                                         # "Reject …?"
    pg.get_by_role("button", name="Reject edge").click()
    pg.wait_for_timeout(2500)
    check("Reject edge (UI) → status rejected", (rel(d["id"], r["rel_id"]) or {}).get("status") == "rejected")
    pg.get_by_role("button", name="Rejected").first.click()
    pg.wait_for_selector("#modalBox input[placeholder^='Filter by entity']")
    pg.fill("#modalBox input[placeholder^='Filter by entity']", d["name"])
    pg.wait_for_timeout(300)
    shown = pg.locator("#modalBox input[data-rej]").count()
    pg.locator("#modalBox label", has_text="select all shown").locator("input").check()
    counter = pg.locator("#modalBox .kv", has_text="shown ·").first.inner_text()
    check("Rejected list: entity filter + 'select all shown' counter", shown >= 1 and f"{shown} selected" in counter, counter)
    dlg.queue.append(True)                                         # "Restore N …?"
    pg.locator("#modalBox button.primary", has_text="Restore").click()
    pg.wait_for_timeout(3000)
    check("Rejected list Restore → back to active", (rel(d["id"], r["rel_id"]) or {}).get("status") == "active")


def test_edit_details_and_revert(pg, dlg):
    d = pick_entity()
    r = first_live(d)
    open_entity(pg, d["name"])
    pg.locator("#relList .item", has_text=f"{r['a']['name']} —{r['rel_type']}→ {r['b']['name']}").first.click()
    pg.locator("#relPane").get_by_role("button", name="Edit", exact=True).click()
    pg.wait_for_selector("#relPane .form")
    sels = pg.locator("#relPane select")                            # type, subtype, status, evidence
    new_ev = "speculative" if r["evidence"] != "speculative" else "inferred"
    sels.nth(3).select_option(new_ev)
    subs = [o for o in sels.nth(1).locator("option").evaluate_all("os => os.map(o => o.value)") if o != "auto"]
    if subs:
        sels.nth(1).select_option(subs[0])
    pg.fill("#relPane textarea", "UI test: mechanism edited in the admin editor.")
    pg.get_by_role("button", name="Save").click()
    pg.wait_for_timeout(3500)
    now = rel(d["id"], r["rel_id"]) or {}
    ok = now.get("evidence") == new_ev and now.get("mechanism") == "UI test: mechanism edited in the admin editor." \
        and (not subs or (now.get("rel_subtype") == subs[0] and now.get("rel_subtype_locked")))
    check("Edit: evidence + mechanism + locked subtype saved", ok,
          f"evidence {now.get('evidence')}, subtype {now.get('rel_subtype')} locked={now.get('rel_subtype_locked')}")
    pg.get_by_role("button", name="Edit log").click()
    pg.wait_for_selector("#modalBox table")
    dlg.queue.append(True)                                         # "Revert …?"
    pg.locator("#modalBox tr", has_text="edit_edge").first.get_by_role("button", name="Revert").click()
    pg.wait_for_timeout(3500)
    back = rel(d["id"], r["rel_id"]) or {}
    check("Edit log Revert → evidence, mechanism and subtype restored",
          back.get("evidence") == r["evidence"] and back.get("mechanism") == r["mechanism"]
          and not back.get("rel_subtype_locked"), f"evidence {back.get('evidence')}")


def test_add_edge(pg, dlg):
    d = pick_entity()
    r = first_live(d)
    open_entity(pg, d["name"])
    pg.locator("#relList .item", has_text=f"{r['a']['name']} —{r['rel_type']}→ {r['b']['name']}").first.click()
    pg.get_by_role("button", name="Add edge on this card").click()
    pg.wait_for_selector("#cardChips button", timeout=20000)
    other = r["b"] if r["a"]["id"] == d["id"] else r["a"]
    pg.locator("#cardChips span.tag", has_text=other["name"]).first.get_by_role("button", name="tgt").click()
    sels = pg.locator("#relPane select")                            # type, subtype, evidence
    vocab = sels.nth(0).locator("option").evaluate_all("os => os.map(o => o.value)")
    new_type = next(t for t in vocab if t != r["rel_type"] and t not in ("partners_with", "competes_with", "benchmarks_against"))
    sels.nth(0).select_option(new_type)
    sels.nth(2).select_option("inferred")
    pg.fill("#relPane textarea", "UI test: an edge the extractor missed, added by hand.")
    dlg.queue.append(True)                                         # "Add … ?"
    pg.get_by_role("button", name="Add edge").last.click()
    pg.wait_for_timeout(3500)
    hit = [x for x in api(f"/entity/{d['id']}")["relationships"] if x["entry_id"] == r["entry_id"]
           and x["rel_type"] == new_type and x["evidence"] == "inferred"]
    check("Add edge on this card → saved with the chosen type + evidence", len(hit) == 1, f"{new_type} on {r['entry_id']}")


def test_delete_guard(pg, dlg):
    target = None                                                   # a small counterpart of a busy entity
    for r in pick_entity()["relationships"]:
        for side in (r["a"], r["b"]):
            dd = api(f"/entity/{side['id']}")
            live = [x for x in dd["relationships"] if x["status"] == "active"]
            if dd["type"] != "group" and 1 <= len(live) <= 2 and not dd["family"]:
                target = dd
                break
        if target:
            break
    if not target:
        check("delete guard (no small entity in snapshot — skipped)", True)
        return
    open_entity(pg, target["name"])
    dlg.queue.append("definitely not the name")                     # the typed-name prompt
    pg.get_by_role("button", name="Delete entity").click()
    pg.wait_for_timeout(1500)
    still = api(f"/entities?limit=50&q={urllib.request.quote(target['name'])}")["entities"]
    check("Delete entity with live edges: wrong typed name → nothing deleted",
          any(x["id"] == target["id"] for x in still))
    dlg.queue.append(target["name"])
    pg.get_by_role("button", name="Delete entity").click()
    pg.wait_for_timeout(3500)
    gone = api(f"/entities?limit=50&q={urllib.request.quote(target['name'])}")["entities"]
    check("…exact typed name → deleted (local copy only)", not any(x["id"] == target["id"] for x in gone))


def test_internal_events_section(pg, dlg):
    who = next((e for e in api("/entities?limit=200&q=")["entities"]
                if api(f"/entity/{e['id']}").get("internal_events")), None)
    if not who:
        check("entity pane internal events (none in snapshot — skipped)", True)
        return
    n = len(api(f"/entity/{who['id']}")["internal_events"])
    open_entity(pg, who["name"])
    check(f"entity pane shows 'Internal events ({n})' for {who['name']}",
          f"Internal events ({n})" in (pg.locator("#entityPane").text_content() or ""))


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page(viewport={"width": 1600, "height": 1000})
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        dlg = Dialogs(pg)
        for fn in (test_boot, test_reject_then_restore, test_edit_details_and_revert, test_add_edge,
                   test_delete_guard, test_internal_events_section):
            try:
                fn(pg, dlg)
            except Exception as exc:  # noqa: BLE001
                check(f"{fn.__name__} ran", False, f"{type(exc).__name__}: {str(exc)[:200]}")
            pg.keyboard.press("Escape")
            pg.evaluate("() => { const m = document.getElementById('modal'); if (m) m.style.display = 'none'; }")
        check("admin page: no page errors", not errors, "; ".join(errors)[:300])
        browser.close()
    failed = [n for n, ok, _d in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed" + (f" — FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
