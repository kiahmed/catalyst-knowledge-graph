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

  // "Supply is concentrated: NVIDIA on 24% of 286 supply links · Money is spread out: top on 4%"
  function takeaway(flows) {
    const ok = Object.entries(flows).filter(([, f]) => f.n >= 10 && f.top);
    if (!ok.length) return "Too few links in this window for a read on concentration.";
    ok.sort((a, b) => b[1].share - a[1].share);
    const [hk, hi] = ok[0], [lk, lo] = ok[ok.length - 1];
    const name = k => FLOWS[k].label[0].toUpperCase() + FLOWS[k].label.slice(1);
    const hiTxt = `${name(hk)} is concentrated: ${hi.top.who} on ${Math.round(100 * hi.share)}% of ${hi.n} ${FLOWS[hk].label} links`;
    return ok.length > 1
      ? `${hiTxt} · ${name(lk)} is spread out: the top company is on ${Math.round(100 * lo.share)}%`
      : hiTxt;
  }

  // d = { nodes:[{id}], edges:[lines], heat:Map, layerOf:Map, second:Map, raised:Map, labels, colors, onPick, windowLabel }
  function render(container, d) {
    container.textContent = "";
    // Rows only in the phone layout (html.is-mobile, the same switch as the rest of the
    // page); a narrow desktop window keeps columns.
    const W = Math.max(320, container.clientWidth);
    const vertical = document.documentElement.classList.contains("is-mobile");
    const members = COLS.map(l => d.nodes.filter(n => d.layerOf.get(n.id) === l)
      .map(n => [n.id, d.heat.get(n.id) || 0]).sort((a, b) => b[1] - a[1]));
    const gov = d.nodes.filter(n => d.layerOf.get(n.id) === "government");
    const flows = computeFlows(d.edges, d.layerOf);
    const heatAll = [...d.heat.values()].reduce((s, v) => s + v, 0) || 1;
    const maxCount = Math.max(1, ...members.map(m => m.length));

    // Geometry. Desktop: 4 columns left→right. Phone: 4 rows top→bottom, each row =
    // header, cloud, then the gap the next arrow + its label use.
    const SHORT = { capital: "Capital", infra: "Chips", maker: "Makers", deployer: "Customers", government: "Government" };
    const takeLines = takeaway(flows).split(" · ").flatMap(t => (vertical ? t.replace(": ", ":\n").split("\n") : [t]));
    const head = 46 + 17 * takeLines.length;
    let H, centers, Rmax, rowTop = [];
    if (vertical) {
      Rmax = Math.min(W * 0.40, 150);
      const rowH = 56 + 2 * Rmax + 100;
      rowTop = COLS.map((_, i) => head + 20 + i * rowH);
      centers = rowTop.map(t => ({ x: W / 2, y: t + 56 + Rmax }));
      H = head + 20 + COLS.length * rowH + 40;
    } else {
      H = Math.max(520, container.clientHeight);
      const span = W / COLS.length;
      const top = head + 70, bottom = H - 70;
      Rmax = Math.min(span * 0.36, (bottom - top) * 0.40);
      centers = COLS.map((_, i) => ({ x: span * (i + 0.5), y: (top + bottom) / 2 }));
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
      const sub = [f.top ? `${def.hub}: ${f.top.who} ${Math.round(100 * f.share)}%` : "",
                   f.outside ? `${f.outside} from outside ${SHORT[def.home]}` : ""].filter(Boolean);
      sub.forEach((t, i) => el(labelsG, "text", { x: lx, y: ly + 15 * (i + 1), "text-anchor": anchor, class: "vc-flow-sub" }, t));
    });

    // Columns: header + cloud.
    const placed = [];
    const hits = (b) => placed.some(p => !(b[2] < p[0] || p[2] < b[0] || b[3] < p[1] || p[3] < b[1]));
    COLS.forEach((l, i) => {
      const C = centers[i], m = members[i], R = radius[i];
      const g = el(svg, "g", { class: "vc-col", "data-layer": l, "data-count": m.length });
      const heat = m.reduce((s, [, h]) => s + h, 0);
      const money = m.reduce((s, [n]) => s + (d.raised.get(n) || 0), 0);
      const hx = vertical ? 14 : C.x, anchor = vertical ? "start" : "middle";
      const hy = vertical ? rowTop[i] + 16 : head + 20;
      el(g, "text", { x: hx, y: hy, "text-anchor": anchor, class: "vc-col-name", fill: d.colors[l] }, d.labels[l]);
      el(g, "text", { x: hx, y: hy + 17, "text-anchor": anchor, class: "vc-col-stat" },
        `${m.length} companies · ${Math.round(100 * heat / heatAll)}% of activity${money ? " · " + fmtUsd(money) + " raised" : ""}`);
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
        if (j < 4) {
          const tw = name.length * 6.4, ty = y - r - 5;
          const box = [x - tw / 2, ty - 11, x + tw / 2, ty + 2];
          if (!hits(box)) { placed.push(box); el(g, "text", { x: x.toFixed(1), y: ty.toFixed(1), "text-anchor": "middle", class: "vc-dot-label" }, name); }
        }
      });
      if (!m.length) el(g, "text", { x: C.x, y: C.y, "text-anchor": "middle", class: "vc-col-stat" }, "none in this window");
      g.setAttribute("data-r", R.toFixed(0));
    });

    // Government strip.
    const regs = d.edges.filter(e => e.rel === "regulates").length;
    el(svg, "text", { x: vertical ? 14 : W / 2, y: H - 30, "text-anchor": vertical ? "start" : "middle",
      class: "vc-col-stat vc-gov", fill: d.colors.government, "data-count": gov.length },
      `${d.labels.government}: ${gov.length} bodies · ${regs} regulatory links`);
    el(svg, "text", { x: vertical ? 14 : W / 2, y: H - 12, "text-anchor": vertical ? "start" : "middle", class: "vc-note" },
      "Dot = company in its main role (ring = a second role) · size & brightness = heat · arrows = links between layers · click a dot to open it on the map");
    return { flows, members: members.map(m => m.length) };
  }

  window.RoboticsChain = { render, computeFlows, COLS };
})();
