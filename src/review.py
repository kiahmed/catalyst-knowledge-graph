"""Review queue — the one place "a human (or an LLM pass) must decide" lands.

Storage is the existing admin inbox, Firestore admin_sessions/attention_needed/
entries (resolved=false), which these existing decision points already write via
upstream_dedup.flag_attention:
  - ingest entity resolution: LLM unsure (< resolution.min_confidence), or a
    legacy alias contradicted (kind entity_ingest_unsure)
  - entity audit apply: less-certain merge / link / alias drop (entity_<claim>)
  - Arboryx dedup feed surprises (dedup_matched_missing, upstream_deleted_unexpected)
plus, surfaced 2026-11-10, the Pass-3 direction verifier's "unclear" verdict
(kind direction_unclear, one entry per relationship).

Deliberately NOT queued: new / discarded names (already decided, logged in
unresolved_entities) and speculative edges (labeled by evidence_type; thousands).

This module only reads / resolves entries; it never decides what needs review.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from .upstream_dedup import ATTENTION_PATH


def _entries(fs: Any):
    col, parent, sub = ATTENTION_PATH
    return fs.collection(col).document(parent).collection(sub)


def list_open(fs: Any) -> list[dict[str, Any]]:
    """Open items, newest first. Each carries its doc `id` plus the stored fields."""
    out = []
    for d in _entries(fs).where("resolved", "==", False).stream():
        item = {k: v for k, v in (d.to_dict() or {}).items()}
        item["id"] = d.id
        if hasattr(item.get("created_at"), "isoformat"):
            item["created_at"] = item["created_at"].isoformat()
        out.append(item)
    out.sort(key=lambda i: str(i.get("created_at") or ""), reverse=True)
    return out


def count_open(fs: Any) -> int:
    return sum(1 for _ in _entries(fs).where("resolved", "==", False).select([]).stream())


def resolve(fs: Any, ids: Iterable[str], *, by: str, resolution: str, edit_ids: Iterable[str] = ()) -> int:
    """Mark entries reviewed. Unknown / already-resolved ids are skipped.
    edit_ids (auto-settle) are stored in full as `settled_by_edits`, so a revert
    of ANY of them can reopen the item; `resolution` is only display text."""
    settled = [e for e in dict.fromkeys(edit_ids) if isinstance(e, str) and e]
    ids = [i for i in dict.fromkeys(ids) if isinstance(i, str) and i]
    n, batch, ref = 0, fs.batch(), _entries(fs)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for i in ids:
        snap = ref.document(i).get()
        if not snap.exists or (snap.to_dict() or {}).get("resolved") is not False:
            continue
        batch.update(ref.document(i), {"resolved": True, "resolved_by": by, "resolved_at": now,
                                       "resolution": (resolution or "reviewed")[:300],
                                       "settled_by_edits": settled})
        n += 1
        if n % 400 == 0:
            batch.commit(); batch = fs.batch()
    if n % 400:
        batch.commit()
    return n


# Which kinds an admin edit may settle, and by what exact target (2026-11-10).
# Anything not listed here (Arboryx dedup kinds, future kinds) is NEVER auto-
# settled — it stays open until someone presses "Reviewed".
_AUTO = {
    "direction_unclear": "rel",          # that relationship was edited
    "humanoid_entity_check": "rel",      # 2026-11-10 LLM pass leftovers: that relationship was edited
    "entity_wrong_alias": "alias",       # that (entity, alias) pair was moved / dropped / spun out
    "entity_ingest_unsure": "entity",    # the entity ingest created was merged / linked / renamed / edited
    "entity_merge": "pair",              # a merge between exactly those two entities
    "entity_subsidiary_of": "pair",      # a parent link between exactly those two entities
    "entity_sister_of": "pair",          # a sister link between exactly those two entities
}


def matching(items: list[dict[str, Any]], *, rel_ids: Iterable[int] = (), entity_names: Iterable[str] = (),
             aliases: Iterable[tuple[str, str]] = (), entity_pairs: Iterable[tuple[str, str]] = ()) -> list[str]:
    """Ids of open items an admin edit just addressed — the SPECIFIC target only:
    a relationship item by its rel_id; an alias item by its (entity, alias)
    pair; an ingest-unsure item by its entity; an audit merge/link item only by
    an edit joining exactly those two entities. Other kinds never match."""
    rel_ids = {int(r) for r in rel_ids if isinstance(r, int) or (isinstance(r, str) and r.isdigit())}
    names = {n.lower() for n in entity_names if n}
    pairs = {(e.lower(), a.lower()) for e, a in aliases if e and a}
    joins = {frozenset((x.lower(), y.lower())) for x, y in entity_pairs if x and y}
    hit = []
    for it in items:
        how = _AUTO.get(str(it.get("kind") or ""))
        ent, other = str(it.get("entity") or "").lower(), str(it.get("other") or "").lower()
        if how == "rel" and it.get("rel_id") is not None and int(it["rel_id"]) in rel_ids:
            hit.append(it["id"])
        elif how == "alias" and (ent, other) in pairs:
            hit.append(it["id"])
        elif how == "entity" and (ent or str(it.get("doc_id") or "").split("~", 1)[0].lower()) in names:
            hit.append(it["id"])
        elif how == "pair" and frozenset((ent, other)) in joins:
            hit.append(it["id"])
    return hit


SETTLED_BY = "settled by admin edit "


def reopen_for_edits(fs: Any, edit_ids: Iterable[str]) -> int:
    """A revert puts the data back, so the items its edit auto-settled are open again."""
    want = {e for e in edit_ids if e}
    if not want:
        return 0
    n, batch = 0, fs.batch()
    for d in _entries(fs).where("resolved", "==", True).stream():
        data = d.to_dict() or {}
        if set(data.get("settled_by_edits") or []) & want:     # the full list, never the truncated text
            batch.update(d.reference, {"resolved": False, "settled_by_edits": [],
                                       "resolution": f"reopened: edit reverted ({data.get('resolution') or ''})"[:300]})
            n += 1
    if n:
        batch.commit()
    return n
