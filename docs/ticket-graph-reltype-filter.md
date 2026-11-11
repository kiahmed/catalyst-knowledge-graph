# Ticket — Graph relationship-type filter (top right of the map)

Status: built 2026-11-11 · Item 5 of the 2026-11-11 delivery list · frontend only

## Requirements
- R1 A dropdown "Filter by relationship" at the top right of the Graph tab (phone: bottom right). The node
  panel overlays it while open.
- R2 Options: "All relationships", then every parent `rel` present in the loaded graph with its edge count,
  each followed by its subtypes (`rel_subtype`, docs/ticket-relationship-subtypes.md) present, indented.
- R3 Parent choice = every edge of that type (subtyped or not). Subtype choice = only edges with that
  subtype. Applied before same-pair aggregation, so "seen in N" counts only matching cards.
- R4 Lives in the URL hash as `rt=<rel>` or `rt=<rel>/<subtype>` (shareable link, like ev/minc/tw);
  "All" removes it. A linked choice absent from the current window stays selectable with (0).
- R5 Same filter everywhere the graph filters edges: graph.js filterPayload (+ render signature), the
  page's window-widening probe (graphEdgeCountFor), node-panel edges (edgesTouching), ego search.
- R6 No other filter or default view changes; with "All" the graph is exactly as before.
