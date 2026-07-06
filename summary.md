> **[SUPERSEDED 2026-04-18]** — This summary captures the original
> proposal-A-vs-proposal-B comparison, written before the product
> pivoted to a **graph UI with colored nodes, edge tooltips, time +
> weight filters, and invalidated-edge overlays**. Mentions of a
> "weekly markdown digest" below describe the *old* proposal A, not
> the current direction. Canonical product + technical direction now
> lives in `docs/technical_spec.md`.

---

                                                                                                         
  Proposal written to catalyst-knowledge-graph/claude_proposal.md. Here's the comparison and MVP recommendation.
                                                                                                                
  Agreement with proposal.md                                                                                
                                                                                                                
  - Competitive landscape is accurate. Bloomberg SPLC is lagging (10-K/customs-driven), AlphaSense is still
  search-centric despite its Sentieo/Tegus/Carousel acquisitions, Dataminr is reactionary, Kensho focuses on    
  event-to-price linkage. Gap is real.                                                                       
  - "Shadow supply chain" is the strongest framing — real-time relationships from press releases/pilots don't   
  hit Bloomberg SPLC for 12–24 months.
  - Narrative velocity as a primitive is the most defensible single feature.                                    
  - M&A sourcing, B2B GTM trigger events, credit risk are legitimate buyer segments.                         
                                                                                                     
  Disagreement with proposal.md                                                                                 
                                                                                                             
  - Jumps too fast to "Neo4j vs ArangoDB." Storage is a weekend decision. The project lives or dies on entity   
  resolution and extraction precision.                                                                          
  - Assumes graph queries are the product. They're not — product is a pushed weekly digest.                  
  - "Democratized graph analytics" has a graveyard — Quid, Diffbot, Primer, Yewno, Neo4j Bloom.                 
  - Ignores the cold-start problem — narrative velocity needs 12–24 months backfill.
  - Treats this as a B2B SaaS vs single-operator research tool — different cost curves.                         
                                                                                                             
  MVP recommendation                                                                                            
                                                                                                                
  One sector, end-to-end, 60-day eval, no infra theater.                                                        
                                                                                                                
  1. Sector: Robotics — ~150–300 entities, high partnership density, already ingested daily.                    
  2. Four layers: Ingestion (exists) → Extraction (closed 15-item vocab) → DuckDB (not Neo4j) → Insight layer
  (chokepoint, narrative velocity, cluster-break detectors → weekly markdown digest).                           
  3. Not in MVP: no multi-tenant, no UI, no real-time alerts, no permissioning, no second sector.               
  4. Success criteria (60 days): ≥2 novel relationships/week, ≥1 defensible chokepoint/month, >85% precision on 
  200 hand-labeled edges.                                                                                       
  5. First ticket (1 week): "Extract closed-vocab relationship triples from existing Arboryx Robotics JSON    
  entries, store in DuckDB, write a daily count query."                                                         
                                                                                                                
  Core insight: Arboryx's sector-focused ingestion is the unfair advantage. Don't dilute it with a "graph of  
  everything" mandate.                                                                                          
                                                                                                                
  Sources cited:                                                                                                
  - https://www.hebbia.com/resources/alphasense-competitors                                                     
  - https://arxiv.org/pdf/2508.00961 (FinKario)                                                                 
  - https://ieeexplore.ieee.org/document/9378471/                                                               
  - https://www.tandfonline.com/doi/full/10.1080/00207543.2025.2575841                                          
  - https://www.gurustartups.com/reports/top-ai-investment-intelligence-platforms-2025                          
                                                                                            