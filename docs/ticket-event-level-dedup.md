# Ticket: event-level dedup — same story emitted as multiple catalyst cards

**Filed by:** soljet-postiz (consumer). **Owner:** catalyst-knowledge-graph.
**Date:** 2026-07-19. **Severity:** medium — cosmetic upstream, but publicly visible
and directly billable downstream.

## Summary

The KG dedups **entities** (canonical company records, aliases, `unresolved_entities`).
It does not appear to dedup **events**. When several outlets cover the same funding
round / acquisition / pivot on different dates, each becomes its own catalyst card
with its own `card_id` and `date`. Downstream those are indistinguishable from
genuinely new catalysts.

## Why this repo, not the publisher

soljet-postiz consumes cards and cannot fix this:

- It sees only `headline` / `entities` / `relationships` per card. Matching on
  headline text would be guesswork, and would misfire on the KG's formulaic
  headline templates (see false-positive note below).
- The signals needed to decide "same event" — `source_refs`, extraction
  provenance, canonical entity ids, relationship shape — live here.
- A publisher silently dropping cards would hide a data problem rather than fix it.

## Impact

Measured over the live `CKG-Robotics/catalysts/items` collection (349 cards):

- **10 clusters, 21 cards** (~6%) look like repeat coverage of one event.
- Publishing the backlog untouched posts the same story up to 3× to the same
  audience, weeks apart, with no acknowledgement it's an update.
- On X this is **billable**: ~$0.20 per post, so each duplicate is real money on a
  pay-per-use balance.

## Evidence

Clustered by ≥2 shared entities **and** ≥70% overlap of distinctive headline words
(stopwords like *robotics/humanoid/secures/million/intensifying* removed).
Confidence labels are ours — **the ambiguous ones need a KG judgement call**:

### Confirmed same event — identical amount, different dates
| card_id | date | headline |
|---|---|---|
| ROB-060426-001 | 2026-06-04 | Generalist AI Secures $400 Million Funding, Intensifying Physical … |
| ROB-071826-004 | 2026-07-18 | Generalist AI Secures $400M to Advance Physical AGI Robotics Found… |

### Likely same event — same story, re-reported as it developed
| card_id | date | headline |
|---|---|---|
| ROB-041726-001 | 2026-04-17 | Faraday Future Pivots to Embodied AI, Targets 1,000 Robot Shipments |
| ROB-051026-005 | 2026-05-10 | Faraday Future Pivots from EVs to Embodied AI Humanoid Robotics |
| ROB-061426-001 | 2026-06-14 | Faraday Future Pivots into Embodied AI with Humanoid and Quadruped |

| ROB-062426-001 | 2026-06-24 | Agility Robotics Goes Public via $2.5 Billion SPAC Merger |
| ROB-071426-002 | 2026-07-14 | Agility Robotics Goes Public Via SPAC, Raising $620 Million for Hu… |

*(Agility: $2.5B is the valuation, $620M the raise — same SPAC, different figure
emphasised. A reader seeing both would reasonably think two events occurred.)*

### Uncertain — amounts differ; may be distinct rounds OR revised reporting
| card_id | date | headline |
|---|---|---|
| ROB-031226-007 | 2026-03-12 | Neura Robotics Secures €1 Billion Funding, Intensifying Humanoid R… |
| ROB-071826-002 | 2026-07-18 | NEURA Robotics Secures $1.4 Billion to Accelerate Physical AI Robo… |

| ROB-041026-002 | 2026-04-10 | Zhongqing Robot Achieves Unicorn Status with $200M Series B Led by… |
| ROB-071826-003 | 2026-07-18 | Humanoid Robotics Startup Achieves Unicorn Status with $150M Serie… |

*(Neura: €1B ≈ $1.08B vs $1.4B — a later round, or the same round restated?
Zhongqing: the July headline doesn't name the company and the amount differs, so
it may be a different startup entirely. Only extraction provenance can settle these.)*

## Known false-positive class (please don't "fix" this)

Headline-similarity alone is **not** a usable signal here, because the KG emits
formulaic templates. These are correctly distinct and must stay distinct:

- "AI2 Robotics Secures $735 Million, Intensifying Humanoid Robot Competition"
- "Apptronik Secures $520 Million Mega-Round, Intensifying Humanoid Robot …"
- "Neura Robotics Secures €1 Billion Funding, Intensifying Humanoid Robot …"

Different companies, different rounds, ~86% word overlap. Any dedup must key on
entity + event identity, not text similarity.

## Suggested direction (KG's call)

1. Derive an **event key** at extraction: `(primary_entity_id, event_type,
   normalised_amount, ~time window)` — e.g. `funding_round:neura:1.4B`.
2. On collision, **update the existing catalyst** (refresh `source_refs`, amount,
   date) instead of creating a new card — so later coverage enriches rather than
   duplicates.
3. If a genuinely new development warrants its own card, link it
   (`supersedes` / `follows_from`) so consumers can render it as an update and
   choose to post only the latest.
4. Whatever the mechanism, expose it on the card so consumers can act on it — a
   `superseded_by` field, or simply omitting stale duplicates from the export.

Option 3 is the most useful to us: soljet-postiz already composes a temporal
lead-in ("Back in mid March: …") and could say "update on X" if the link existed.

## What soljet-postiz will do meanwhile

Nothing automatic — we won't guess. The backlog will be posted as-is unless this
lands first, and the operator will eyeball obvious repeats. No publisher-side
dedup is planned, deliberately.

## Repro

```python
# from soljet-postiz repo root
from src.lib.config_loader import load_tier
from bin._common import build_source, load_dotenv
load_dotenv()
t = load_tier("arboryx.robotics")
rows = build_source(t.sources[0], t).list_recent(since=<epoch>, limit=5000)
# cluster on: >=2 shared entity names AND >=70% overlap of headline words
# after removing {robotics, robot, humanoid, secures, launches, million,
# billion, intensifying, competition, targets, production, industrial, scale, funding}
```
