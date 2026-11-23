"""Data-cleanup checklist — ONE living file, rebuilt in place from prod (read-only, no LLM, no writes to the DB).

    make cleanup-checklist        →  data/cleanup_checklist.md

Sections (each item has a stable key, so your ticks survive every rebuild):
  A. Concept / non-organization entities (the list from the 2026-11-11 LLM label pass — the only LLM step,
     already done; this tool re-checks those entities against prod, it doesn't re-label).
  B. Aliases that pull cards onto an entity those cards never name (fresh scan).
  C. Generic aliases ("humanoid robots", "Gulf investors"…) sitting on real (non-group) entities (fresh scan).
  D. Layer healing (technical_spec §2.9c): companies whose supply-chain layer is doubtful (top-2 role scores
     within 20%) or missing (no role link) — `make layers-heal APPLY=1` asks the AI; ticks itself once decided.
  E. Open work items — HAND-MAINTAINED (deploy follow-ups, bugs, decisions pending). Never generated: every
     rebuild copies section E's lines exactly as they are.

An item is ticked when you mark it `- [x]`, or automatically when prod shows it's resolved: in A the entity is
gone / a group / has no live edges; in B and C an item the scan no longer finds stays in the list, ticked, marked
"resolved". Your ticks are permanent. Automatic ticks carry an `auto` flag in the item's marker and are
recomputed on every rebuild — so an item that comes back (an entity gains a new edge, an alias pulls cards again)
re-opens instead of staying falsely ticked. The file is replaced atomically (it is the only store of your ticks).
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb  # noqa: E402

from src import duckdb_sync, entities, layers, names  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "data"
OUT = DATA / "cleanup_checklist.md"
SEED = DATA / "concept_entities_labels.txt"       # the one-time LLM label list (section A's source)
LIVE = "status IN ('active', 'materialized')"
# Keys are machine-made: section + entity id (+ a short hash of the alias) — never free text, so no spaces and
# no "-->" can ever appear inside one.
_KEY = re.compile(r"<!--\s*key:([A-Za-z]+:[0-9]+(?::[0-9a-f]{10})?)(\s+auto)?\s*-->")
_TICK = re.compile(r"^\s*[-*+]\s+\[[xX]\]")
_RESOLVED = " — resolved (no longer found by the scan)"


def _alias_key(section: str, eid: int, alias: str) -> str:
    """Exact alias text (case kept) → two aliases differing only by case get their own keys."""
    return f"{section}:{eid}:{hashlib.sha1(alias.strip().encode()).hexdigest()[:10]}"


def _legacy_key(section: str, eid: int, alias: str) -> str:
    """The pre-2026-11-19 key (lower-cased alias) — still honoured so earlier ticks aren't lost."""
    return f"{section}:{eid}:{hashlib.sha1(alias.strip().lower().encode()).hexdigest()[:10]}"


_SEC_E: list[str] = []      # section E's raw lines from the current file (filled by _previous)


def _previous() -> tuple[set[str], dict[str, tuple[str, str]], list[str]]:
    """From the current checklist: ticked keys; {key: (section letter, item text)}; section A's raw lines."""
    ticked: set[str] = set()
    items: dict[str, tuple[str, str]] = {}
    sec_a: list[str] = []
    global _SEC_E
    _SEC_E = []
    if OUT.exists():
        section = ""
        for line in OUT.read_text().splitlines():
            if line.startswith("## "):
                section = line[3:4]
                continue
            if section == "E":                            # hand-maintained: kept verbatim, keys or not
                _SEC_E.append(line)
                continue
            found = list(_KEY.finditer(line))     # the LAST marker is the real one; notes after it are allowed
            if not found:
                continue
            m = found[-1]
            if _TICK.match(line) and not m.group(2):     # only YOUR ticks carry over; auto ticks are recomputed
                ticked.add(m.group(1))
            text = re.sub(r"^\s*[-*+]\s+\[[ xX]\]\s*", "", line[:m.start()]).rstrip()
            items[m.group(1)] = (section, text)
            if section == "A":
                sec_a.append(line)
    return ticked, items, sec_a


def _item(done: bool, key: str, text: str, *, ticked: set[str] | None = None, auto: bool = False) -> str:
    """`ticked` = your ticks; `auto` = prod shows it resolved. Yours wins (permanent); an auto-only tick is
    flagged so the next rebuild recomputes it."""
    human = key in (ticked or set())
    flag = " auto" if (auto and not human) else ""
    return f"- [{'x' if (done or human or auto) else ' '}] {text} <!-- key:{key}{flag} -->"


def _concepts(con, ticked, prev_a) -> tuple[list[str], str]:
    if not SEED.exists():   # never drop section A (and its ticks) because the source list is missing this run
        return prev_a, f"⚠ source list {SEED.name} missing — section A kept as it was."
    seed_done, out, skipped = set(), [], 0
    for line in SEED.read_text().splitlines():
        m = re.match(r"\s*\[(\w+)\]\s+(.+?) \(id (\d+), ", line)
        if not m:
            if line.strip().startswith("["):
                skipped += 1
            continue
        kind, name, eid = m.group(1), m.group(2), int(m.group(3))
        key = f"concept:{eid}"
        if kind.lower() == "done":
            seed_done.add(key)
        row = con.execute("SELECT name, type FROM entities WHERE entity_id = ?", [eid]).fetchone()
        live = con.execute(f"SELECT COUNT(*) FROM relationships WHERE ? IN (entity_a_id, entity_b_id) AND {LIVE}",
                           [eid]).fetchone()[0] if row else 0
        if not row:
            state, auto = "gone (deleted / merged)", True
        elif row[1] == "group":
            state, auto = "now a group bucket", True
        elif live == 0:
            state, auto = "no live edges left", True
        else:
            state, auto = f"{live} live edge(s)", False
        label = kind if kind.lower() != "done" else "concept"
        out.append(_item(False, key, f"**{row[0] if row else name}** (id {eid}) — {label} — {state}",
                         ticked=ticked | seed_done, auto=auto))
    return out, (f"⚠ {skipped} line(s) in {SEED.name} couldn't be read and are not listed." if skipped else "")


def _carry_resolved(section: str, found: set[str], items, ticked, legacy: set[str] = frozenset()) -> list[str]:
    """B/C items listed last time that the scan no longer finds: keep them, ticked, marked resolved.
    `legacy` = old-style keys of items still found (re-keyed, not resolved) — not carried."""
    out = []
    for key, (sec, text) in items.items():
        if sec == section and key not in found and key not in legacy:
            out.append(_item(False, key, text.replace(_RESOLVED, "") + _RESOLVED, ticked=ticked, auto=True))
    return out


def _aliases(con, ticked, items) -> list[str]:
    out, found, legacy = [], set(), set()
    for eid, name, alias in con.execute("""SELECT e.entity_id, e.name, a.alias FROM entity_aliases a JOIN entities e USING (entity_id)
                                           WHERE lower(a.alias) <> lower(e.name) ORDER BY e.name, a.alias""").fetchall():
        cards = []
        for entry, text in con.execute(f"""SELECT DISTINCT c.entry_id, coalesce(c.headline, '') || ' ' || coalesce(c.raw_finding, '')
                FROM relationships r JOIN catalysts c USING (catalyst_id) WHERE ? IN (r.entity_a_id, r.entity_b_id) AND r.{LIVE}
                AND lower(coalesce(c.headline, '') || ' ' || coalesce(c.raw_finding, '')) LIKE ? ORDER BY 1""",
                [eid, f"%{alias.lower()}%"]).fetchall():
            if entities.only_via_alias(text, name, alias):
                cards.append(entry)
        if cards:
            key = _alias_key("alias", eid, alias)
            found.add(key)
            legacy.add(_legacy_key("alias", eid, alias))
            shown = ", ".join(cards[:6]) + (f" … +{len(cards) - 6}" if len(cards) > 6 else "")
            out.append(_item(False, key, f"**{name}** ← alias “{alias}” — {len(cards)} card(s) never name {name}: {shown}",
                             ticked=ticked | ({key} if _legacy_key("alias", eid, alias) in ticked else set())))
    return out + _carry_resolved("B", found, items, ticked, legacy)


def _generic_aliases(con, ticked, items) -> list[str]:
    out, found, legacy = [], set(), set()
    for eid, name, alias in con.execute("""SELECT e.entity_id, e.name, a.alias FROM entity_aliases a JOIN entities e USING (entity_id)
                                           WHERE e.type <> 'group' ORDER BY e.name, a.alias""").fetchall():
        if names.is_generic(alias):
            key = _alias_key("generic", eid, alias)
            found.add(key)
            legacy.add(_legacy_key("generic", eid, alias))
            out.append(_item(False, key, f"**{name}** ← generic alias “{alias}” (move to a group bucket or drop)",
                             ticked=ticked | ({key} if _legacy_key("generic", eid, alias) in ticked else set())))
    return out + _carry_resolved("C", found, items, ticked, legacy)


def _write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".cleanup_checklist.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)             # mkstemp makes 0600; keep it editable by a non-root user
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _layer_candidates(con, ticked, items) -> list[str]:
    out, found = [], set()
    for c in layers.candidates(con):                       # doubtful first, then most-linked
        key = f"layer:{c['entity_id']}"
        found.add(key)
        sc = ", ".join(f"{k} {v:g}" for k, v in sorted(c["scores"].items(), key=lambda x: -x[1])) or "no role links"
        out.append(_item(False, key, f"**{c['name']}** (id {c['entity_id']}) — {c['why']} — role {c['role_layer']} "
                                     f"({sc}) · {c['edges']} live links", ticked=ticked))
    return out + _carry_resolved("D", found, items, ticked)


def build(con) -> tuple[str, list[str]]:
    """The checklist text for this DB + the current file's ticks; also the per-section open counts."""
    ticked, items, prev_a = _previous()
    a_items, a_note = _concepts(con, ticked, prev_a)
    secs = [("A. Concept / non-organization entities", "Fix in admin: make it a `group`, merge it into the real company, "
             "or delete it with its edges. Auto-ticked when gone / a group / no live edges.", a_items, a_note),
            ("B. Aliases pulling in cards that never name the entity", "Fix in admin: entity → tick those cards' edges → "
             "spin out / move / drop the alias.", _aliases(con, ticked, items), ""),
            ("C. Generic aliases on real entities", "Fix in admin: move the alias to the matching group bucket, or drop it.",
             _generic_aliases(con, ticked, items), ""),
            ("D. Layer healing — doubtful / no-role companies", "Run `make layers-heal APPLY=1` (the AI decides each "
             "company's ROLE from its own links + headlines, up to 200 per run); spot-check the doubtful answers first and "
             "fix wrong ones by hand. Ticks itself once decided (or once its roles stop being doubtful).",
             _layer_candidates(con, ticked, items), "")]
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    lines = ["# Data-cleanup checklist", "",
             f"Rebuilt {stamp} from prod by `make cleanup-checklist` (read-only). Tick `- [x]` as you go; ticks are kept on",
             "every rebuild, and items prod shows as resolved tick themselves. This is the only cleanup file — rebuild it,",
             "don't make new ones.", ""]
    is_open = re.compile(r"^\s*[-*+]\s+\[ \]")
    counts = []
    for title, how, its, note in secs:
        open_n = sum(1 for i in its if is_open.match(i))
        counts.append(f"{title.split('.')[0]} {open_n}/{len(its)} open")
        lines += [f"## {title} — {open_n} open of {len(its)}", "", how, ""] + ([note, ""] if note else []) \
            + (its or ["- nothing found"]) + [""]
    body_e = list(_SEC_E)
    while body_e and not body_e[-1].strip():
        body_e.pop()
    while body_e and not body_e[0].strip():
        body_e.pop(0)
    counts.append(f"E {sum(1 for x in body_e if is_open.match(x))} open (hand-kept)")
    lines += ["## E. Open work items (hand-maintained — kept exactly as is on every rebuild)", ""] \
        + (body_e or ["Add items as `- [ ] …`; tick `- [x]` when done."]) + [""]
    return "\n".join(lines), counts


def main() -> int:
    p = os.path.join(tempfile.mkdtemp(), "robotics.duckdb")
    duckdb_sync.pull(p)
    con = duckdb.connect(p, read_only=True)
    text, counts = build(con)
    _write_atomic(OUT, text)
    print(f"{OUT}: " + ", ".join(counts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
