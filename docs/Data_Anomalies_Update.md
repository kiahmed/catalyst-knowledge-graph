# Data Anomalies Update

Living register of data conflicts, missing evidence and interpretation issues discovered while using Catalyst Knowledge Graph data.

Created and last updated: **2026-11-01 (America/New_York)**.

The founder authorized creation and ongoing updates of this document. This documentation exception does not authorize edits to product code, Firestore data, migrations, dependencies or Git state. Recording an issue does not mean the underlying data has been corrected.

## Maintenance rules

- Append new findings with a stable ID; update the existing entry when the same issue recurs.
- Distinguish confirmed source conflicts, interpretation errors, suspected inconsistencies and retrieval gaps.
- Record affected card/edge IDs, observed values, source evidence, impact, containment and proposed follow-up.
- A retrieval miss is not proof that information is absent from the entire database. A source that does not establish a claim is not proof the claim is false.
- Preserve history. Close an entry only with an identified correction and verification evidence; never silently remove it.
- Do not include credentials, tokens or unnecessary personal information.

## Register

| ID | Finding | Evidence classification | Status |
|---|---|---|---|
| DA-001 | Hyundai/Boston Dynamics capacity target represented as annual deployments | Confirmed source conflict | Open; excluded from pilot reply evidence |
| DA-002 | Card record date used as an announcement date | Confirmed downstream interpretation error | Open; draft withdrawn and prompt corrected |
| DA-003 | Agility/GXO deployment relationship directions differ across cards | Suspected schema/normalization inconsistency | Open; needs semantic review |
| DA-004 | Pilot retrieval did not establish requested deployment/integration evidence | Retrieval coverage gap; database-wide absence unconfirmed | Open; investigate coverage and retrieval |
| DA-005 | Graph URL cannot exclude a specific disputed card or flagged edges independently | Confirmed inspected UI filtering limitation | Open; Traction checks the entire selected view locally |

## DA-001 — Manufacturing capacity versus annual deployments

- **Discovered:** 2026-11-01 during Traction Engine's five-post pilot.
- **Affected card:** `ROB-072526-003`; entities: Hyundai Motor Group and Boston Dynamics/Atlas.
- **Observed headline:** `Hyundai Group Commits to 30,000 Annual Atlas Humanoid Robot Deployments by 2028`.
- **Observed relationship:** `Hyundai Motor Group --deploys--> Boston Dynamics`, marked `direct`, confidence `1`. Its mechanism describes a roadmap for 30,000 Atlas units annually in production environments by 2028.
- **Verified distinction:** Hyundai's primary announcement describes a target for manufacturing capacity of 30,000 robot units annually by 2028. This does not substantiate an equivalent count of annual Atlas deployments or completed operational installations.
- **Impact:** A high-confidence graph record produced a misleading numerical response candidate. Extraction confidence did not prevent a mismatch between production capacity and deployment volume.
- **Containment:** Traction Engine excluded this card from the pilot's response evidence and withdrew the numerical draft. The local exclusion is tied to the reviewed snapshot. Product data remains unchanged.
- **Proposed follow-up:** Review the headline, relationship mechanism and metric classification against the primary source. Keep planned capacity, shipments, pilots and operating deployments distinct; identify which assertions actually have supporting citations. Changes require the product's authorized correction workflow.
- **Resolution:** Not corrected in the product; no claim of a database-wide audit.

Primary evidence: [Hyundai's CES 2026 AI Robotics Strategy](https://www.hyundaimotorgroup.com/en/news/hyundai-motor-group-announces-ai-robotics-strategy--to-lead-human-centered-robotics-era-at-ces-2026).

## DA-002 — Record date versus announcement date

- **Affected input:** The same card has `date: 2026-07-25`.
- **Observed failure:** A downstream generated draft called this a July 25, 2026 announcement, without evidence establishing that event date.
- **Verified distinction:** The primary release describes a January 5, 2026 announcement; the website displays January 6 in KST. A card date alone does not establish source publication or event timing.
- **Classification:** The unsupported announcement-date claim is confirmed. Whether the card's date field is itself incorrect remains unconfirmed until its intended semantics are checked.
- **Containment:** The draft was withdrawn. Traction's evidence-review prompt now prohibits converting an internal record date into an announcement date without explicit support.
- **Proposed follow-up:** Document and, where necessary, separate event, source-publication and ingestion/record dates. Do not overwrite a field until its existing contract is verified.
- **Resolution:** Product date semantics still require review.

Primary evidence: [Hyundai's dated release](https://www.hyundaimotorgroup.com/en/news/hyundai-motor-group-announces-ai-robotics-strategy--to-lead-human-centered-robotics-era-at-ces-2026).

## DA-003 — Deployment edge direction needs clarification

Observed in the read-only snapshot:

| Card | From | Relationship | To |
|---|---|---|---|
| `ROB-070726-002` | Agility Robotics | `deploys` | GXO Logistics |
| `ROB-102826-006` | GXO Logistics | `deploys` | Agility Robotics |

- **Classification:** Confirmed difference in direction; whether either record violates the product schema is not yet established.
- **Impact:** A consumer cannot safely translate every `A --deploys--> B` edge into “A deploys technology from B.” The same label may be representing a vendor deploying at a customer or a customer deploying a vendor's technology.
- **Containment:** Traction's evidence reviewer is instructed to use the mechanism and sources, rather than infer company roles from the verb label alone.
- **Proposed follow-up:** Verify the intended relationship contract, inspect source-supported actor roles and normalize or qualify the affected edges through the product's own workflow. Do not automatically reverse edges.
- **Resolution:** Unreviewed; no product data changed.

## DA-004 — Missing evidence in pilot retrieval results

The five-post pilot did not retrieve sufficient cited evidence for:

- Lenovo/China Southern Power Grid operational inspection sites, deployment scale and documented organizational roles.
- Lenovo humanoid pick-and-pack customer pilots, routine deployments and integration partners.
- GXO's Statesville/Lebanon site-specific commissioning, layout/integration differences and operating reliability.
- Goldfinch AI connector compatibility, production integration performance or the advertised engineering-effort/ROI claims.

These are **retrieval gaps**, not confirmed absence from Firestore. Entity/lexical matching may have missed relevant records. Do not infer that the public claims are false or that the product can never answer them.

- **Proposed follow-up:** Compare targeted entity/relationship retrieval with hybrid retrieval, inspect provenance and coverage, and mark any genuinely absent data separately from retrieval failures.
- **Resolution:** Open; the reviewed evidence did not support replies addressing those specific claims.

## Related verified context — RMAC

[Boston Dynamics' September 21, 2026 announcement](https://bostondynamics.com/news/boston-dynamics-opens-robotics-metaplant-application-center-to-train-humanoid-robots-for-manufacturing-tasks) describes RMAC as an Atlas training center/testbed. This supports a distinction between training milestones and demonstrated production operation; the release alone does not establish WMS/MES/ERP performance or ROI. It does not prove that no other operational evidence exists.

The pilot's surviving draft cites this primary source instead of the conflicting card.

## DA-005 — Destination filters must be checked against the visible graph

- **Discovered:** 2026-11-01 while preparing a first-party Boston Dynamics graph destination.
- **Observed UI behavior:** The inspected graph renderer supports time window (`tw`), explicit-window enforcement (`twx`), minimum confidence (`minc`), evidence types (`ev`), invalidation visibility (`inv`) and company focus (`q`). Its edge filter does not independently reject `flagged` edges, and no inspected URL parameter excludes a particular card ID.
- **Impact:** Rejecting a card in Traction does not automatically remove its relationships from a graph destination. A high minimum confidence alone does not remove a conflicting edge with confidence 1.
- **Containment:** Traction validates the graph slice locally and rejects a candidate view if the visible slice includes a known conflict, flagged edge or unresolved citations. The selected Boston Dynamics view uses `q=Boston Dynamics`, `tw=30`, `twx=1`, `minc=0.75`, `ev=inferred`, `inv=0`; four inferred competitive edges passed checks in the saved snapshot. The disputed direct deployment card is excluded by this view's evidence/date filters.
- **Limitation:** A live graph URL displays the current published payload; the saved-snapshot check does not guarantee the contents of future payloads. Confidence is metadata, not a factual guarantee, and inferred links remain hypotheses.
- **Proposed follow-up:** Consider first-class flagged/conflicting-evidence filtering and explicitly scoped graph destinations through the product's authorized change process. No product code or data changed.

## Audit references

- Traction Engine: `reports/apify-pilot.md` and `reports/apify-pilot.json` — findings, targets, evidence-fit decisions and current draft.
- Traction Engine: `data/pilot-source-verification.json` — primary-source checks and reviewed snapshot fingerprint.
- Findings above reflect the inspected snapshot and selected pilot retrieval, not a complete product-data audit.

## Change history

| Date | Update |
|---|---|
| 2026-11-01 | Created by founder request; recorded DA-001 through DA-004, with confirmed issues distinguished from investigations and retrieval gaps. |
| 2026-11-01 | Added DA-005 after checking the independent Boston Dynamics graph destination against the inspected UI filters and saved snapshot. |
