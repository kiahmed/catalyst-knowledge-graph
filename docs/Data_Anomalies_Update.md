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

## DA-006 — Government entity aliases absorb distinct organizations (2026-11-06)

- **Verified observation:** In the November 6 snapshot (`generated_at=2026-11-06T13:34:06Z`), entity `20`, canonical `China`, type `government`, includes `China Southern Power Grid`, `China Telecom`, `Sequoia Capital China` and other organization names in `aliases`.
- **Observed retrieval consequence:** Exact alias resolution for the saved Lenovo/China Southern Power Grid discussion produced the filter `China, Lenovo`. The search then returned broad China industrial-policy cards and expanded through China's relationships. This does not establish the specific utility's identity or its partnership with Lenovo.
- **Containment:** Traction Engine now requires identity review before using an alias that resolves to a government entity for company filtering. Canonical exact-name matches remain distinct. The affected search is rerun with cached public query embeddings; no CKG data is changed.
- **Follow-up:** Product-owner review of these aliases and the affected source relationships. This observation concerns the inspected snapshot; it does not claim a new product correction has been deployed.

## DA-007 — AEON/Digit product conflation (2026-11-07)

- **Verified snapshot mismatch:** `ROB-102826-006` contains a Schaeffler AG → Agility Robotics deployment mechanism describing “1,000 AEON humanoids (Agility's Digit)”. Hexagon's primary announcement identifies the 1,000-unit, seven-year program as Hexagon AEON, not Digit. This does not disprove separate Schaeffler–Agility activity; it invalidates this particular attribution.
- **Primary source checked:** https://hexagon.com/about/news-media/press-releases/2026/hexagon-schaeffler-deploy-aeon-humanoids-across-global-factory-network (April 22, 2026).
- **Containment:** Excluded the card from this evidence-fit review's destinations and graph view. Product data unchanged.

## DA-008 — Fox/FORT endpoint and mechanism mismatch (2026-11-07)

- **Verified internal mismatch:** `ROB-100226-006` names `Fox Robotics` as the supplier to Agility Robotics, while the mechanism and headline describe `FORT Robotics` safety architecture. A further inferred edge uses the same Fox endpoint with a FORT mechanism.
- **Scope:** Endpoint/text inconsistency verified in the November 6 saved snapshot. Underlying safety certification/product claims were not independently verified here; no claim that a correction is already deployed.
- **Containment:** Excluded this card from reply evidence and destination selection. Record requires product-owner identity/source review; no CKG data changed.

## DA-009 — SEER/Serve endpoint and mechanism mismatch (2026-11-07)

- **Verified internal mismatch:** In the November 6 saved snapshot, `ROB-080726-005` is titled “SEER Robotics, Foxconn, NVIDIA Unveil Humanoid Robot with Advanced AI at GTC” and names SEER's SRC-series controller in its mechanisms, but relationship endpoints use `Serve Robotics` (including Foxconn supplies Serve Robotics and Serve Robotics supplies Foxconn).
- **Scope:** This is a verified inconsistency between the stored headline/mechanism and entity endpoint. The underlying deployment and manufacturing claims have not been independently verified in this review. It does not establish the correct replacement identity or direction for every edge.
- **Containment:** Exclude this card from new Traction reply evidence pending product-owner review. No product data, code or indexes changed.

## DA-010 — Assured Robot Intelligence acquisition mapped to Intel (2026-11-07)

- **Verified internal mismatch:** `ROB-110126-001` contains `Meta Platforms acquires Intel`, while its mechanism says Meta acquired Assured Robot Intelligence in May 2026. Those are different named acquisition targets within the same stored record.
- **Scope:** Endpoint/mechanism inconsistency verified in the November 6 snapshot. The underlying acquisition and other claims in this card were not independently verified here. Do not infer an acquisition of Intel from this record.
- **Containment:** Exclude the card from new Traction reply destinations pending identity/source review. Product data and code remain unchanged.

## DA-011 — Nyobolt supply/partnership edges lack bound source references (2026-11-07)

- **Verified provenance gap:** `ROB-101726-004` (Nyobolt supplies Agility Robotics) and `ROB-101526-003` (Agility Robotics partners_with Nyobolt) have empty `source_refs` in the inspected snapshot despite card-level source URLs. The published graph contains these edges, but Traction cannot resolve an edge-bound citation through the existing reference list.
- **Independent public corroboration:** Nyobolt's October 15, 2026 announcement identifies it as the battery technology provider for Agility Digit 5: https://nyobolt.com/resources/news/agility-robotics-selects-nyobolt-as-its-ultra-fast-charging-battery-technology-partner-for-digit-5/ . This supports the supplier/partnership identity, not demonstrated operating economics or independent performance validation.
- **Containment:** Any Traction reply using these links must retain explicit local primary-source checks tied to the exact card, endpoints and relationship. Local verification does not repair the product's missing citations; no CKG writes were made.

## DA-012 — Samsung SDS resolves to Samsung SDI (2026-11-07)

- **Verified snapshot observation:** The November 6 entity export names Samsung SDI with aliases `Samsung` and `Samsung SDS`. In the saved Korean supply-chain discussion, exact alias resolution converted the focal name Samsung SDS into entity 34, Samsung SDI, while Wuji remained unresolved.
- **Suspected identity conflation / verified retrieval consequence:** The selected discussion concerns Samsung SDS and Wuji, but the vector filter used Samsung SDI. Returned cards therefore cannot establish the intended partnership without identity review. This records the alias and resulting filter, not an independently verified claim about the reported partnership.
- **Containment:** Hold this reply candidate; do not use the Samsung SDI results as evidence for Samsung SDS. Other extracted angles remain saved. Product-owner review of aliases and affected relationships is needed; no product data or code changed.

## DA-013 — Generative Bionics funding label, chip claim and endpoint gaps (2026-11-07)

- **Verified mismatch in the inspected November 6 snapshot:** `ROB-102126-002` describes AMD Ventures participating in a €70 million **Series C** round. Generative Bionics' October 21 company release calls this a **seed** round. The same card's AMD compute mechanism adds “AI5-class chips”; the checked announcement supports AMD Helios infrastructure but does not establish that chip label. Treat the chip detail as unsupported by the checked source, not a verified alternative specification.
- **Verified internal identity mismatch:** `ROB-102426-001` has a Generative Bionics → Renishaw Plc partnership edge whose mechanism describes **Eni**, not Renishaw. Other names in that card (`Ernij Next`, `Venture Capital`) require identity review; their correctness is not established here.
- **Primary source:** https://news.gbionics.ai/?article=data-factory&lang=en ; company-issued syndication: https://www.prnewswire.co.uk/news-releases/generative-bionics-to-build-europes-largest-physical-ai-data-factory-and-advance-european-humanoid-sovereignty-with-amd-ai-infrastructure-302914884.html . It describes a planned data-factory network, not demonstrated training performance.
- **Containment:** The NEURA-discussion reply uses only the locally corroborated Generative Bionics → AMD partnership from `ROB-102426-001`. Its graph excludes `ROB-102126-002`, uses direct evidence and minimum confidence 0.85, and was locally checked to suppress the other lower-confidence relationships. Do not use the whole card as a validated destination. The selected partnership still lacks bound `source_refs`; Traction retains an exact local source attestation. No product data or code changed.


## DA-014 — Amazon acquisition mechanisms mapped to AWS; Kiva historical coverage gap (2026-11-15)

- **Verified internal mismatch:** In the November 15 Traction snapshot, `ROB-032226-001` is titled “Amazon Acquires RIVR, Accelerating Autonomous Last-Mile Delivery with Physical AI”. Its acquisition mechanisms name Amazon, but endpoints use `Amazon Web Services` for RIVR and Kiva Systems. The Kiva mechanism explicitly describes Amazon acquiring Kiva in 2012; the card date is 2026-03-22. Card date must not be treated as the Kiva event date.
- **Bounded coverage observation:** Kiva occurs in only this card’s relationship endpoints in the inspected snapshot. No earlier Kiva relationship was found there; this is not proof that no pre-acquisition indicators ever existed.
- **Retrieval consequence:** Exact AND on Amazon, Bain Capital Ventures and Kiva Systems returns zero cards before semantic ranking. This establishes a filter outcome, not a completed temporal search or independent validation of historical claims.
- **Follow-up:** Product-owner review of Amazon/AWS attribution and historical event dates. No upstream data or code changed; no external fact-check or new collection performed.

## DA-015 — UBTECH automotive deployments mapped to European Union (2026-11-22)

- **Verified internal mismatch:** In the Traction snapshot generated on November 16, `ROB-040826-002`, `ROB-070126-002`, `ROB-071926-005` and `ROB-091726-003` each contain a `European Union deploys UBTECH` relationship whose mechanism instead identifies **NIO** as the automotive manufacturer or deployment site. The September 17 card specifically describes NIO's Hefei assembly line.
- **Scope:** This verifies a mismatch between saved relationship endpoints and saved mechanism text. It does not independently verify the NIO deployment claim or establish the correct replacement endpoint for every record.
- **Traction consequence:** The November 22 agent shadow review held a humanoid-versus-industrial-arm reply partly because this attribution could not support the selected comparison. Do not cite these European Union edges as NIO deployment evidence before product-owner review. No CKG data, code or indexes were changed.

## DA-016 — Schaeffler/Humanoid actuator contract conflated with inferred robot volume (2026-11-22)

- **Verified current-record observation:** A read-only Firestore GET of card `ROB-051326-003` on November 22 still returns a direct `Schaeffler AG supplies Humanoid` mechanism saying Schaeffler contracted to supply actuators for **up to 100,000 robots** by 2031. Its `deploys` mechanism says **over 1,000 robots**. The November 16 Traction snapshot has the same text.
- **Primary-source check:** Humanoid's May 13 announcement states a binding, phased deployment targeting a **four-digit number of wheeled units by 2032**, plus a separate five-year preferred-supplier agreement for **more than half of Humanoid's wheeled-platform joint actuator demand through 2031**, expected to total a **seven-digit number of actuators**. It does not state a contracted robot-volume equivalent of 100,000; that appears to be an extrapolation from actuator quantity, not a direct agreement term. Source: https://thehumanoid.ai/humanoid-secures-landmark-deal-with-schaeffler-to-deploy-thousands-of-humanoid-robots/ . Reuters separately reports an estimated 1,000–2,000 planned robots, attributed to a Humanoid spokesperson, and says precise numbers were not disclosed: https://www.marketscreener.com/news/humanoid-to-deploy-up-to-2-000-robots-at-schaeffler-plants-ce7f5bdcd98df424 .
- **Traction consequence:** The saved agent-handoff draft for LinkedIn post `7527510161851297792` repeated the unsupported “100,000 robots” contract framing. Do not publish that exact draft or use `ROB-051326-003` as its product destination until the card's mechanism and evidence label are reviewed. A separate card, `ROB-053026-002`, records the planned 1,000–2,000 deployment without the actuator-volume extrapolation. No CKG data, code, dependency, migration or Git state was changed.
