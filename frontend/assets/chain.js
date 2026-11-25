// Value-chain view (technical_spec.md §2.9c part 2). Draws the Graph tab's
// current view as supply-chain columns — Capital → Chips, parts & platforms →
// Robot & AI makers → Customers & deployers (Government as a strip) — each a
// cloud of its companies (hottest in the middle, size + brightness = heat), with
// arrowed ribbons for the links that flow between layers.
//
// Flows count relationships by TYPE, whatever column a company sits in, so a
// multi-role company shows up in every flow it takes part in (NVIDIA's
// investments are in the money ribbon although its dot sits in Chips). The
// page computes the inputs; this file only lays out and draws.
(function () {
  "use strict";
  const NS = "http://www.w3.org/2000/svg";
  const COLS = ["capital", "infra", "maker", "deployer"];
  // kind → from/to column, label, home layer of its givers, what the hub is called
  const FLOWS = {
    money: { a: 0, b: 2, label: "money", home: "capital", hub: "top investor" },
    supply: { a: 1, b: 2, label: "supply", home: "infra", hub: "top supplier" },
    deploy: { a: 2, b: 3, label: "deployment", home: "maker", hub: "most deployed" },
  };

  function el(parent, tag, attrs, text) {
    const e = document.createElementNS(NS, tag);
    Object.entries(attrs || {}).forEach(([k, v]) => e.setAttribute(k, v));
    if (text != null) e.textContent = text;
    parent.appendChild(e);
    return e;
  }
  const fmtUsd = v => (v >= 1e9 ? `$${(v / 1e9).toFixed(1)}B` : v >= 1e6 ? `$${Math.round(v / 1e6)}M` : `$${Math.round(v / 1e3)}K`);

  // One flow record per unique line (filterPayload already aggregated restatements).
  // Hub = the company on the giving side: investor, supplier, deployed vendor.
  function computeFlows(edges, layerOf) {
    const out = {};
    Object.keys(FLOWS).forEach(k => { out[k] = { n: 0, outside: 0, hub: new Map() }; });
    edges.forEach(e => {
      let kind, giver;
      if (e.rel === "invests_in") { kind = "money"; giver = e.from; }
      else if (e.rel === "supplies") { kind = "supply"; giver = e.from; }
      else if (e.rel === "built_on") { kind = "supply"; giver = e.to; }            // substrate supplies the app
      else if (e.rel === "deploys" || e.rel === "pilots") { kind = "deploy"; giver = e.to; }   // vendor → customer
      else return;
      const f = out[kind];
      f.n += 1;
      f.hub.set(giver, (f.hub.get(giver) || 0) + 1);
      if (layerOf.get(giver) !== FLOWS[kind].home) f.outside += 1;
    });
    Object.values(out).forEach(f => {
      let top = null;
      f.hub.forEach((n, who) => { if (!top || n > top.n) top = { who, n }; });
      f.top = top;
      f.share = top && f.n ? top.n / f.n : 0;
    });
    return out;
  }

  // One rule for "who leads a layer", used by the cloud centre, its label, its sentence and the
  // left panel: the layer's own companies counted on that layer's own role links.
  // KEEP IN SYNC with computeFlows' rel/giver mapping above: ribbon "N from <layer> companies"
  // (= f.n - f.outside) must equal layerStats(l).links — guarded by test_value_chain.
  const ROLE = {
    capital: [["invests_in", "from"]], infra: [["supplies", "from"], ["built_on", "to"]],
    maker: [["deploys", "to"], ["pilots", "to"]], deployer: [["deploys", "from"], ["pilots", "from"]],
    government: [["regulates", "from"]],
  };
  const WORDS = {   // who, what, verb, single
    capital: ["investors", "investments", "made the most", "funder"],
    infra: ["suppliers", "supply links", "supplies the most", "supplier"],
    maker: ["makers", "deployments & pilots", "was deployed the most", "maker"],
    deployer: ["customers", "deployments & pilots", "deployed the most", "customer"],
    government: ["bodies", "regulatory links", "regulates the most", "regulator"],
  };
  const SHORT = { capital: "Capital", infra: "Chips", maker: "Makers", deployer: "Customers", government: "Government" };
  function layerStats(edges, layerOf) {
    const out = {};
    Object.keys(ROLE).forEach(l => {
      const counts = new Map();
      let links = 0;
      ROLE[l].forEach(([rel, side]) => edges.forEach(e => {
        if (e.rel !== rel || layerOf.get(e[side]) !== l) return;
        counts.set(e[side], (counts.get(e[side]) || 0) + 1); links += 1;
      }));
      let top = null;
      counts.forEach((n, who) => { if (!top || n > top.n) top = { who, n }; });
      out[l] = { links, givers: counts.size, counts, top, share: top && links ? top.n / links : 0 };
    });
    return out;
  }
  // "206 investors made 235 investments. SoftBank made the most (12). The other 223 came from
  //  205 others, so no single funder dominates."
  function sentence(l, st) {
    const [who, what, verb, single] = WORDS[l];
    if (!st || !st.links) return `No ${what} in this window.`;
    const t = st.top, pct = Math.round(100 * st.share);
    const rest = st.links - t.n, others = st.givers - 1;
    return `${st.givers} ${who} · ${st.links} ${what}. ${t.who} ${verb} (${t.n}).`
      + (others ? ` The other ${rest} came from ${others} other${others === 1 ? "" : "s"}` : "")
      + (st.share >= 0.15 ? `${others ? "," : ""} so ${pct}% rides on ${t.who}.` : `${others ? "," : ""} so no single ${single} dominates.`);
  }

  // d = { nodes:[{id}], edges:[lines], heat:Map, layerOf:Map, second:Map, raised:Map, labels, colors, onPick, windowLabel }
  function render(container, d) {
    container.textContent = "";
    // Rows only in the phone layout (html.is-mobile, the same switch as the rest of the
    // page); a narrow desktop window keeps columns.
    const W = Math.max(320, container.clientWidth);
    const vertical = document.documentElement.classList.contains("is-mobile");
    const LAYOUT = [...COLS, "government"];                 // government = small cloud on its own row
    const stats = layerStats(d.edges, d.layerOf);
    const members = LAYOUT.map(l => d.nodes.filter(n => d.layerOf.get(n.id) === l)
      .map(n => [n.id, d.heat.get(n.id) || 0])
      .sort((a, b) => ((stats[l].counts.get(b[0]) || 0) - (stats[l].counts.get(a[0]) || 0)) || b[1] - a[1]));
    const flows = computeFlows(d.edges, d.layerOf);
    const heatAll = [...d.heat.values()].reduce((s, v) => s + v, 0) || 1;
    const maxCount = Math.max(1, ...members.slice(0, 4).map(m => m.length));

    // Geometry. Desktop: 4 columns left→right. Phone: 4 rows top→bottom, each row =
    // header, cloud, then the gap the next arrow + its label use.
    const ranked = COLS.filter(l => stats[l].links >= 10).sort((a, b) => stats[b].share - stats[a].share);
    const tk = [];
    if (ranked.length) {
      const hi = stats[ranked[0]], lo = stats[ranked[ranked.length - 1]];
      const L0 = ranked[0], L1 = ranked[ranked.length - 1];
      tk.push(`Most concentrated: ${hi.top.who} has ${hi.top.n} of the ${hi.links} ${WORDS[L0][1]} from ${SHORT[L0]} companies (${Math.round(100 * hi.share)}%).`);
      if (ranked.length > 1) tk.push(`Most spread out: ${SHORT[L1]} — the top one, ${lo.top.who}, has only ${lo.top.n} of ${lo.links} ${WORDS[L1][1]} (${Math.round(100 * lo.share)}%).`);
    } else tk.push("Too few links in this window for a read on concentration.");
    const wrap = (t, n) => t.split(" ").reduce((ls, w) => { const last = ls[ls.length - 1];
      if (last && (last + " " + w).length <= n) ls[ls.length - 1] = last + " " + w; else ls.push(w); return ls; }, []);
    const takeLines = tk.flatMap(t => (vertical ? wrap(t, Math.floor(W / 7.2)) : [t]));
    const head = 46 + 17 * takeLines.length;
    let H, centers, Rmax, rowTop = [];
    if (vertical) {
      Rmax = Math.min(W * 0.40, 150);
      const rowH = 56 + 2 * Rmax + 100;   // + 50 per row for the sentence
      rowTop = LAYOUT.map((_, i) => head + 20 + i * (rowH + 50));
      centers = rowTop.map(t => ({ x: W / 2, y: t + 106 + Rmax }));
      H = head + 20 + LAYOUT.length * (rowH + 50) + 40;
    } else {
      const row1 = Math.min(640, Math.max(470, container.clientHeight - 60));   // capped: government row follows close below
      const span = W / COLS.length;
      const top = head + 135, bottom = row1 - 40;
      Rmax = Math.min(span * 0.36, (bottom - top) * 0.44);
      centers = COLS.map((_, i) => ({ x: span * (i + 0.5), y: (top + bottom) / 2 }));
      const govTop = bottom + 70;                               // second row: Government
      centers.push({ x: W / 2, y: govTop + 120 + Rmax * 0.4 });
      H = govTop + 120 + Rmax * 0.8 + 60;
    }
    const step = Rmax / Math.sqrt(maxCount);                  // same spacing everywhere: bigger cloud = more companies
    const radius = members.map(m => Math.max(step, step * Math.sqrt(m.length)) + 4);

    const svg = el(container, "svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, class: "vc-svg" });
    const defs = el(svg, "defs");
    COLS.forEach(l => {
      const m = el(defs, "marker", { id: `vc-arrow-${l}`, viewBox: "0 0 10 10", refX: 8, refY: 5,
        markerWidth: 14, markerHeight: 14, markerUnits: "userSpaceOnUse", orient: "auto-start-reverse" });
      el(m, "path", { d: "M0,0 L10,5 L0,10 z", fill: d.colors[l] });
    });
    const x0 = vertical ? 14 : 24;
    el(svg, "text", { x: x0, y: 28, class: "vc-title" }, `Robotics value chain · ${d.windowLabel}`);
    takeLines.forEach((line, i) => el(svg, "text", { x: x0, y: 50 + 17 * i, class: "vc-takeaway" }, line));

    // Ribbons first (under the clouds), labels on top.
    const ribbons = el(svg, "g", { class: "vc-ribbons" });
    const labelsG = el(svg, "g", {});
    Object.entries(FLOWS).forEach(([kind, def]) => {
      const f = flows[kind];
      if (!f.n) return;
      const A = centers[def.a], B = centers[def.b], ra = radius[def.a], rb = radius[def.b];
      const color = d.colors[COLS[def.a]];
      const w = Math.max(3, Math.min(26, 3 + Math.sqrt(f.n) * 1.4));
      let path, lx, ly, anchor = "middle";
      if (def.b - def.a === 1) {                                    // neighbours: straight arrow in the gap
        if (vertical) {
          const y1 = A.y + ra + 8, y2 = rowTop[def.b] - 4;
          path = `M${A.x},${y1} L${A.x},${y2}`;
          lx = A.x + w / 2 + 12; ly = (y1 + y2) / 2 - 6; anchor = "start";
        } else {
          const x1 = A.x + ra + 8, x2 = B.x - rb - 12;
          path = `M${x1},${A.y} L${x2},${A.y}`;
          lx = (A.x + B.x) / 2; ly = A.y - Rmax - 34;               // above the clouds, centred on the gap
        }
      } else {                                                      // Capital → Makers: around the Chips cloud
        if (vertical) {
          const p1 = [A.x + ra * 0.8, A.y + ra * 0.6], p2 = [B.x + rb * 0.8 + 4, B.y - rb * 0.6 - 4];
          const cx = W - 6;
          path = `M${p1[0]},${p1[1]} Q${cx},${centers[1].y} ${p2[0]},${p2[1]}`;
          lx = W - 18; ly = (A.y + ra + rowTop[1]) / 2; anchor = "end";   // in the Capital→Chips gap
        } else {
          const p1 = [A.x + ra * 0.7, A.y + ra * 0.7], p2 = [B.x - rb * 0.7 - 4, B.y + rb * 0.7 + 4];
          const cy = Math.min(H - 60, centers[1].y + Math.max(radius[1], ra, rb) + 50);
          path = `M${p1[0]},${p1[1]} Q${centers[1].x},${cy} ${p2[0]},${p2[1]}`;
          lx = centers[1].x; ly = 0.25 * p1[1] + 0.5 * cy + 0.25 * p2[1] + 24;   // just under the curve's midpoint
        }
      }
      el(ribbons, "path", { d: path, fill: "none", stroke: color, "stroke-width": w, "stroke-opacity": 0.28,
        "stroke-linecap": "butt", class: "vc-flow", "data-kind": kind, "data-n": f.n,
        "marker-end": `url(#vc-arrow-${COLS[def.a]})` });
      el(labelsG, "text", { x: lx, y: ly, "text-anchor": anchor, class: "vc-flow-label", "data-kind": kind }, `${f.n} ${def.label} links`);
      // total = the layer's own links (same number as its cloud sentence / takeaway) + links from others
      const sub = [f.outside ? `${f.n - f.outside} from ${SHORT[def.home]} companies · ${f.outside} from others` : ""].filter(Boolean);
      sub.forEach((t, i) => el(labelsG, "text", { x: lx, y: ly + 15 * (i + 1), "text-anchor": anchor, class: "vc-flow-sub" }, t));
    });

    // Clouds: header, stats, plain sentence; the leader sits in the centre and is named
    // just outside the cloud with a thin line to its dot.
    LAYOUT.forEach((l, i) => {
      const C = centers[i], m = members[i], R = radius[i];
      const g = el(svg, "g", { class: "vc-col", "data-layer": l, "data-count": m.length });
      const heat = m.reduce((s, [, h]) => s + h, 0);
      const money = m.reduce((s, [n]) => s + (d.raised.get(n) || 0), 0);
      const isGov = l === "government";
      const hx = vertical ? 14 : C.x, anchor = vertical ? "start" : "middle";
      const hy = vertical ? rowTop[i] + 16 : (isGov ? C.y - R - 112 : head + 20);
      el(g, "text", { x: hx, y: hy, "text-anchor": anchor, class: "vc-col-name", fill: d.colors[l] }, d.labels[l]);
      el(g, "text", { x: hx, y: hy + 17, "text-anchor": anchor, class: "vc-col-stat" },
        `${m.length} ${isGov ? "bodies" : "companies"} · ${Math.round(100 * heat / heatAll)}% of activity${money ? " · " + fmtUsd(money) + " raised" : ""}`);
      const width = vertical ? W - 28 : (isGov ? Math.min(W - 40, 520) : W / COLS.length - 16);
      wrap(sentence(l, stats[l]), Math.floor(width / 6.3)).slice(0, 4).forEach((line, k) =>
        el(g, "text", { x: hx, y: hy + 36 + 14 * k, "text-anchor": anchor, class: "vc-sentence" }, line));
      m.forEach(([name, h], j) => {
        const ang = j * 2.39996, rad = step * Math.sqrt(j);
        const x = C.x + rad * Math.cos(ang), y = C.y + rad * Math.sin(ang);
        const r = Math.max(2.2, Math.min(step * 0.48 + 14 * h, 3 + 18 * h));
        const second = d.second.get(name);
        const dot = el(g, "circle", { cx: x.toFixed(1), cy: y.toFixed(1), r: r.toFixed(1), fill: d.colors[l],
          "fill-opacity": (0.3 + 0.7 * h).toFixed(2), class: "vc-dot", "data-name": name,
          stroke: second ? d.colors[second] : "#fff", "stroke-width": second ? 2 : 0.6 });
        el(dot, "title", {}, `${name} · ${d.labels[l]}${second ? " + " + d.labels[second] : ""} · heat ${Math.round(100 * h)}%`
          + (d.raised.get(name) ? ` · ${fmtUsd(d.raised.get(name))} raised` : ""));
        dot.addEventListener("click", () => d.onPick(name));
      });
      if (m.length) {                                        // leader: named outside the cloud, top-right
        const lx = C.x + R * 0.75 + 8, ly = C.y - R * 0.75 - 10;
        el(g, "line", { x1: C.x, y1: C.y, x2: lx - 2, y2: ly + 3, class: "vc-leader" });
        el(g, "text", { x: lx, y: ly, "text-anchor": "start", class: "vc-top-label", "data-name": m[0][0] }, m[0][0]);
      } else {
        el(g, "text", { x: C.x, y: C.y, "text-anchor": "middle", class: "vc-col-stat" }, "none in this window");
      }
      g.setAttribute("data-r", R.toFixed(0));
    });

    el(svg, "text", { x: vertical ? 14 : W / 2, y: H - 12, "text-anchor": vertical ? "start" : "middle", class: "vc-note" },
      "Dot = company in its main role (ring = a second role) · size & brightness = heat · arrows = links between layers · click a dot to open it on the map");
    return { flows, members: members.map(m => m.length) };
  }

  window.RoboticsChain = { render, computeFlows, layerStats, sentence, COLS };
})();
