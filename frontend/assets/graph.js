(function () {
  'use strict';

  const EVIDENCE_COLORS = {
    direct: '#1c1917',
    web_grounded: '#10b981',
    inferred: '#d97706',
    speculative: '#ef4444',
  };

  const STATUS_MUTED = '#a8a29e';
  // HSL kept for the CSS legend gradient (browsers lerp linear-gradient
  // in sRGB regardless of color-space input). JS node fills lerp in RGB
  // via HEAT_COOL_RGB / HEAT_WARM_RGB below — HSL hue lerp would walk
  // the wheel through green at mid-heat.
  const HEAT_COOL = { h: 201, s: 47, l: 78 };
  const HEAT_WARM = { h: 10, s: 100, l: 64 };
  const HEAT_COOL_RGB = { r: 173, g: 207, b: 225 };
  const HEAT_WARM_RGB = { r: 255, g: 102, b: 71 };

  const NODE_MIN_PX = 30;
  const NODE_MAX_PX = 90;
  const EDGE_MIN_W = 1;
  const EDGE_MAX_W = 5;
  const MECH_TRUNC = 160;
  const CHUNK_NODES = 15;
  // Views this small get their own quick force layout (readable, always fits
  // the screen) instead of full-history preset positions, which scatter a
  // small subset across the whole canvas (2026-10-27: Tesollo off-screen).
  const SMALL_LAYOUT_NODES = 400;
  // Progressive loading: elements appended per animation frame.
  const PROGRESSIVE_BATCH = 400;
  // "Spread out" (right-click declump): cap how many neighbors fan out
  // around a clicked node, and how many on-screen nodes an empty-canvas
  // click will relax (perf guard on a huge "All" view).
  const SPREAD_MAX_NEIGHBORS = 10;
  const SPREAD_MAX_BACKGROUND_NODES = 400;
  // Locked "obstacle" nodes around a node-centered spread. Uncapped, a hub
  // node's padded box can sweep in most of the graph — bounding this keeps
  // the layout call (and its iteration count) from scaling with graph size.
  const SPREAD_MAX_OBSTACLES = 60;

  const state = {
    cy: null,
    container: null,
    tipEl: null,
    legendEl: null,
    callbacks: {},
    hasFitted: false,
    bilkentRegistered: false,
    renderToken: 0,
    lastSignature: null,
    paused: false,
    loading: false,
    spreading: false,
    activeSpreadLayout: null,
    spreadLockedNodes: null,
  };

  // A real render always supersedes a cosmetic "spread out" in progress:
  // stop its layout and unlock whatever it locked, so a fresh render's
  // elements().remove() doesn't fight an animating layout or leave nodes
  // stuck locked after their elements are long gone.
  function stopActiveSpread() {
    if (state.activeSpreadLayout) {
      try { state.activeSpreadLayout.stop(); } catch (_) {}
      state.activeSpreadLayout = null;
    }
    if (state.spreadLockedNodes) {
      try { state.spreadLockedNodes.unlock(); } catch (_) {}
      state.spreadLockedNodes = null;
    }
    state.spreading = false;
  }

  function lerp(a, b, t) { return a + (b - a) * t; }

  function heatToHex(heat) {
    const t = Math.max(0, Math.min(1, heat || 0));
    const r = Math.round(lerp(HEAT_COOL_RGB.r, HEAT_WARM_RGB.r, t));
    const g = Math.round(lerp(HEAT_COOL_RGB.g, HEAT_WARM_RGB.g, t));
    const b = Math.round(lerp(HEAT_COOL_RGB.b, HEAT_WARM_RGB.b, t));
    const hx = n => n.toString(16).padStart(2, '0');
    return `#${hx(r)}${hx(g)}${hx(b)}`;
  }

  function hslToHex(h, s, l) {
    s /= 100; l /= 100;
    const k = n => (n + h / 30) % 12;
    const a = s * Math.min(l, 1 - l);
    const f = n => {
      const col = l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
      return Math.round(255 * col).toString(16).padStart(2, '0');
    };
    return `#${f(0)}${f(8)}${f(4)}`;
  }

  function truncate(s, n) {
    if (!s) return '';
    return s.length > n ? s.slice(0, n - 1) + '…' : s;
  }

  function parseDate(s) {
    if (!s) return null;
    const d = new Date(s);
    return isNaN(d.getTime()) ? null : d;
  }

  function ensureLayoutRegistered() {
    if (state.bilkentRegistered) return 'cose-bilkent';
    if (typeof window.cytoscape !== 'function') return 'cose';
    if (typeof window.cytoscapeCoseBilkent === 'function') {
      try {
        window.cytoscape.use(window.cytoscapeCoseBilkent);
        state.bilkentRegistered = true;
        return 'cose-bilkent';
      } catch (_) {
        return 'cose';
      }
    }
    if (!state.bilkentRegistered) {
      console.warn('[RoboticsGraph] cytoscape-cose-bilkent not found; falling back to cose layout');
    }
    return 'cose';
  }

  function layoutOpts(name) {
    return {
      name,
      randomize: false,
      animate: false,
      idealEdgeLength: 120,
      nodeRepulsion: 4500,
      fit: true,
      padding: 40,
      tile: false,
      nodeDimensionsIncludeLabels: true,
    };
  }

  function filterPayload(payload, filters) {
    const f = filters || {};
    const timeWindowDays = f.timeWindowDays == null ? 30 : f.timeWindowDays;
    const minConfidence = f.minConfidence == null ? 0 : f.minConfidence;
    const showInvalidated = !!f.showInvalidated;
    const evidenceTypes = new Set(f.evidenceTypes || ['direct', 'inferred', 'web_grounded', 'speculative']);

    const today = parseDate(payload.generated_at) || new Date('2026-04-18');
    const cutoff = timeWindowDays > 0 ? new Date(today.getTime() - timeWindowDays * 86400000) : null;

    const rawEdges = (payload.graph && payload.graph.edges) || [];
    let edges = rawEdges.filter(e => {
      if (!showInvalidated && e.status === 'invalidated') return false;
      if ((e.confidence || 0) < minConfidence) return false;
      if (!evidenceTypes.has(e.evidence_type)) return false;
      if (cutoff) {
        const d = parseDate(e.catalyst_date);
        if (d && d < cutoff) return false;
      }
      return true;
    });

    // Ego view (entity search): only the searched entity's 1-hop edges,
    // capped to the most recent by catalyst_date. Applied on top of the
    // regular filters above.
    if (f.egoEntity) {
      let ego = edges.filter(e => e.from === f.egoEntity || e.to === f.egoEntity);
      const maxE = f.egoMaxEdges == null ? 20 : f.egoMaxEdges;
      if (ego.length > maxE) {
        ego = ego.slice().sort((a, b) =>
          String(b.catalyst_date || '').localeCompare(String(a.catalyst_date || '')));
        ego = ego.slice(0, maxE);
      }
      edges = ego;
    }

    const keep = new Set();
    edges.forEach(e => { keep.add(e.from); keep.add(e.to); });
    if (f.egoEntity) keep.add(f.egoEntity); // show the entity even edge-less
    const rawNodes = (payload.graph && payload.graph.nodes) || [];
    const nodes = rawNodes.filter(n => keep.has(n.id));

    return { nodes, edges };
  }

  function hasPresetPosition(n) {
    return Number.isFinite(n.x) && Number.isFinite(n.y);
  }

  // True when enough nodes carry exporter-provided x/y layout coordinates
  // to skip the force layout. Old payloads lack them entirely.
  function mostHavePositions(nodes) {
    if (!nodes.length) return false;
    let withPos = 0;
    nodes.forEach(n => { if (hasPresetPosition(n)) withPos += 1; });
    return withPos / nodes.length >= 0.9;
  }

  function buildElements(nodes, edges, usePositions, idPrefix) {
    const pfx = idPrefix || '';
    const maxEdgeCount = nodes.reduce((m, n) => Math.max(m, n.edge_count || 0), 1);
    const denom = Math.max(1, maxEdgeCount - 1);

    const els = [];
    nodes.forEach(n => {
      const ec = Math.max(1, n.edge_count || 1);
      const t = Math.min(1, (ec - 1) / denom);
      const size = lerp(NODE_MIN_PX, NODE_MAX_PX, t);
      const label = n.ticker ? `${n.name} (${n.ticker})` : n.name;
      const el = {
        group: 'nodes',
        data: {
          id: n.id,
          label,
          ticker: n.ticker || '',
          type: n.type || '',
          heat: n.heat || 0,
          edgeCount: ec,
          _size: size,
          _fill: heatToHex(n.heat || 0),
        },
      };
      if (usePositions && hasPresetPosition(n)) {
        el.position = { x: n.x, y: n.y };
      }
      els.push(el);
    });

    // Group parallel edges by unordered node-pair so we can fan them out
    // instead of stacking (Cytoscape's default bezier bundles overlap).
    const pairBuckets = new Map();
    edges.forEach((e, idx) => {
      const key = [e.from, e.to].sort().join('\u241F');
      if (!pairBuckets.has(key)) pairBuckets.set(key, []);
      pairBuckets.get(key).push(idx);
    });
    const pairIndexByEdge = new Array(edges.length);
    const pairCountByEdge = new Array(edges.length);
    pairBuckets.forEach(arr => {
      arr.forEach((edgeIdx, i) => {
        pairIndexByEdge[edgeIdx] = i;
        pairCountByEdge[edgeIdx] = arr.length;
      });
    });

    edges.forEach((e, idx) => {
      const conf = Math.max(0, Math.min(1, e.confidence || 0));
      const width = lerp(EDGE_MIN_W, EDGE_MAX_W, conf);
      const pCount = pairCountByEdge[idx];
      const pIdx = pairIndexByEdge[idx];
      // Symmetric fan-out: edges at distances {-60, -30, 0, 30, 60, …}.
      const cpDist = pCount > 1 ? (pIdx - (pCount - 1) / 2) * 40 : 0;
      const evType = e.evidence_type || 'direct';
      const baseColor = EVIDENCE_COLORS[evType] || EVIDENCE_COLORS.direct;
      const status = e.status || 'active';
      let color = baseColor;
      let opacity = 1;
      let lineStyle = 'solid';
      let lineDashPattern = null;
      let zIndex = 10;
      let midLabel = '';

      if (evType === 'speculative') {
        lineStyle = 'dashed';
        lineDashPattern = [4, 3];
      }
      if (status === 'materialized') {
        lineStyle = 'solid';
        lineDashPattern = null;
        midLabel = '✓';
      } else if (status === 'invalidated') {
        color = STATUS_MUTED;
        lineStyle = 'dashed';
        lineDashPattern = [6, 4];
        opacity = 0.3;
      } else if (status === 'stale') {
        opacity = 0.4;
        zIndex = 1;
      }

      els.push({
        group: 'edges',
        data: {
          id: `${pfx}e${idx}-${e.from}-${e.to}-${e.catalyst_id || idx}`,
          source: e.from,
          target: e.to,
          rel: e.rel || '',
          confidence: conf,
          evidenceType: evType,
          mechanism: e.mechanism || '',
          status,
          flagged: !!e.flagged,
          sourceRefs: Array.isArray(e.source_refs) ? e.source_refs : [],
          catalystId: e.catalyst_id || '',
          catalystDate: e.catalyst_date || '',
          _width: width,
          _color: color,
          _opacity: opacity,
          _lineStyle: lineStyle,
          _lineDash: lineDashPattern,
          _zIndex: zIndex,
          _midLabel: midLabel,
          _cpDist: cpDist,
          _cpWeight: 0.5,
        },
      });
    });

    return els;
  }

  function stylesheet() {
    return [
      {
        selector: 'node',
        style: {
          'width': 'data(_size)',
          'height': 'data(_size)',
          'background-color': 'data(_fill)',
          'border-width': 2,
          'border-color': '#ddd8cf',
          'label': 'data(label)',
          'font-family': 'Inter, sans-serif',
          'font-size': 11,
          'font-weight': 500,
          'color': '#57534e',
          'text-valign': 'bottom',
          'text-halign': 'center',
          'text-margin-y': 6,
          'text-wrap': 'wrap',
          'text-max-width': 140,
          'z-index': 10,
        },
      },
      {
        selector: 'node:selected',
        style: {
          'border-color': '#0ea5e9',
          'border-width': 3,
        },
      },
      {
        selector: 'node.robotics-hover',
        style: {
          'border-color': '#0ea5e9',
          'border-width': 3,
          'z-index': 999,
        },
      },
      {
        selector: 'node.robotics-ego',
        style: {
          'border-color': '#06b6d4',
          'border-width': 4,
          'underlay-color': '#06b6d4',
          'underlay-opacity': 0.15,
          'underlay-padding': 10,
          'z-index': 999,
        },
      },
      {
        selector: 'node.robotics-dim',
        style: {
          'opacity': 0.35,
        },
      },
      {
        selector: 'edge',
        style: {
          'width': 'data(_width)',
          'line-color': 'data(_color)',
          'target-arrow-color': 'data(_color)',
          'target-arrow-shape': 'triangle',
          'target-arrow-fill': 'filled',
          'curve-style': 'unbundled-bezier',
          'control-point-distances': 'data(_cpDist)',
          'control-point-weights': 'data(_cpWeight)',
          'line-style': 'data(_lineStyle)',
          'opacity': 'data(_opacity)',
          'z-index': 'data(_zIndex)',
          'arrow-scale': 1.1,
          'label': 'data(_midLabel)',
          'font-family': 'Inter, sans-serif',
          'font-size': 14,
          'font-weight': 700,
          'color': '#10b981',
          'text-background-color': '#ffffff',
          'text-background-opacity': 0.9,
          'text-background-padding': 2,
          'text-background-shape': 'roundrectangle',
        },
      },
      {
        selector: 'edge.robotics-hover',
        style: {
          'opacity': 1,
          'width': 'mapData(confidence, 0, 1, 2, 7)',
          'label': 'data(rel)',
          'font-size': 11,
          'font-weight': 500,
          'color': '#1c1917',
          'text-rotation': 'autorotate',
          'text-margin-y': -8,
          'z-index': 998,
        },
      },
      {
        selector: 'edge.robotics-dim',
        style: {
          'opacity': 0.1,
        },
      },
    ];
  }

  function createTip(container) {
    const el = document.createElement('div');
    el.className = 'robotics-edge-tip';
    el.innerHTML = '<div class="robotics-tip-title"></div><div class="robotics-tip-meta"></div><div class="robotics-tip-mech"></div><div class="robotics-tip-cites"></div>';
    container.appendChild(el);
    return el;
  }

  function createLegend(container) {
    const el = document.createElement('div');
    el.className = 'robotics-graph-legend';
    const coolHsl = `hsl(${HEAT_COOL.h},${HEAT_COOL.s}%,${HEAT_COOL.l}%)`;
    const warmHsl = `hsl(${HEAT_WARM.h},${HEAT_WARM.s}%,${HEAT_WARM.l}%)`;
    const gradientStyle = `background: linear-gradient(90deg, ${coolHsl}, ${warmHsl});`;
    el.innerHTML = [
      '<div class="robotics-legend-section">',
      '  <div class="robotics-legend-title">Nodes</div>',
      '  <div class="robotics-legend-row robotics-legend-heatrow">',
      '    <span class="robotics-legend-heatlabel">low</span>',
      `    <span class="robotics-legend-gradient" style="${gradientStyle}"></span>`,
      '    <span class="robotics-legend-heatlabel">high</span>',
      '  </div>',
      '  <div class="robotics-legend-caption">heat = sum of incident edge confidences</div>',
      '  <div class="robotics-legend-row robotics-legend-sizerow">',
      '    <span class="robotics-legend-dot robotics-legend-dot-sm"></span>',
      '    <span class="robotics-legend-dot robotics-legend-dot-md"></span>',
      '    <span class="robotics-legend-dot robotics-legend-dot-lg"></span>',
      '    <span class="robotics-legend-caption" style="margin:0 0 0 6px">size = edge count</span>',
      '  </div>',
      '</div>',
      '<div class="robotics-legend-section">',
      '  <div class="robotics-legend-title">Evidence</div>',
      '  <div class="robotics-legend-row" style="color:#1c1917"><span class="robotics-legend-swatch"></span><span class="robotics-legend-label">direct</span></div>',
      '  <div class="robotics-legend-row" style="color:#10b981"><span class="robotics-legend-swatch"></span><span class="robotics-legend-label">web_grounded</span></div>',
      '  <div class="robotics-legend-row" style="color:#d97706"><span class="robotics-legend-swatch"></span><span class="robotics-legend-label">inferred</span></div>',
      '  <div class="robotics-legend-row" style="color:#ef4444"><span class="robotics-legend-swatch is-dashed"></span><span class="robotics-legend-label">speculative</span></div>',
      '</div>',
      '<div class="robotics-legend-section">',
      '  <div class="robotics-legend-title">Status</div>',
      '  <div class="robotics-legend-row" style="color:#1c1917"><span class="robotics-legend-swatch"></span><span class="robotics-legend-label">active</span></div>',
      '  <div class="robotics-legend-row" style="color:#1c1917"><span class="robotics-legend-swatch"></span><span class="robotics-legend-check">✓</span><span class="robotics-legend-label">materialized</span></div>',
      '  <div class="robotics-legend-row" style="color:#a8a29e"><span class="robotics-legend-swatch is-dashed"></span><span class="robotics-legend-label">invalidated</span></div>',
      '  <div class="robotics-legend-row" style="color:#a8a29e; opacity:0.6"><span class="robotics-legend-swatch"></span><span class="robotics-legend-label">stale</span></div>',
      '</div>',
    ].join('');
    container.appendChild(el);
    return el;
  }

  // Discrete +/- zoom stepper (same approach as the mini-graph's), stepping
  // multiplicatively about the viewport centre within cy's min/max zoom.
  function createZoomControl(container, cy) {
    const el = document.createElement('div');
    el.className = 'robotics-zoom';
    el.innerHTML = '<button type="button" data-dir="1" title="Zoom in">+</button><button type="button" data-dir="-1" title="Zoom out">−</button>';
    const btns = el.querySelectorAll('button');
    const STEP = 1.5;
    const sync = () => {
      btns[0].disabled = cy.zoom() >= cy.maxZoom() / 1.01;
      btns[1].disabled = cy.zoom() <= cy.minZoom() * 1.01;
    };
    el.addEventListener('click', ev => {
      const b = ev.target.closest('button');
      if (!b) return;
      const level = cy.zoom() * (b.dataset.dir === '1' ? STEP : 1 / STEP);
      cy.zoom({
        level: Math.max(cy.minZoom(), Math.min(cy.maxZoom(), level)),
        renderedPosition: { x: container.clientWidth / 2, y: container.clientHeight / 2 },
      });
      sync();
    });
    cy.on('zoom', sync);
    container.appendChild(el);
    sync();
    return el;
  }

  function showTip(edge, event) {
    if (!state.tipEl) return;
    const d = edge.data();
    state.tipEl.querySelector('.robotics-tip-title').textContent =
      `${d.source} —[${d.rel}]→ ${d.target}`;
    state.tipEl.querySelector('.robotics-tip-meta').textContent =
      `evidence: ${d.evidenceType} · confidence: ${Number(d.confidence).toFixed(2)}`;
    state.tipEl.querySelector('.robotics-tip-mech').textContent =
      truncate(d.mechanism, MECH_TRUNC);
    const n = (d.sourceRefs && d.sourceRefs.length) || 0;
    state.tipEl.querySelector('.robotics-tip-cites').textContent =
      `cites: ${n} source${n === 1 ? '' : 's'}`;
    state.tipEl.classList.add('is-visible');
    positionTip(event);
  }

  function positionTip(event) {
    if (!state.tipEl || !state.container) return;
    const rect = state.container.getBoundingClientRect();
    const oe = event && event.originalEvent;
    if (!oe) return;
    const x = oe.clientX - rect.left;
    const y = oe.clientY - rect.top;
    state.tipEl.style.left = `${x}px`;
    state.tipEl.style.top = `${y}px`;
  }

  function hideTip() {
    if (!state.tipEl) return;
    state.tipEl.classList.remove('is-visible');
  }

  function wireEvents(cy) {
    cy.on('mouseover', 'edge', evt => {
      const edge = evt.target;
      edge.addClass('robotics-hover');
      const connected = edge.connectedNodes();
      cy.elements().addClass('robotics-dim');
      edge.removeClass('robotics-dim');
      connected.removeClass('robotics-dim');
      connected.connectedEdges().forEach(e => {
        if (e.id() !== edge.id()) e.removeClass('robotics-dim');
      });
      showTip(edge, evt);
    });
    cy.on('mousemove', 'edge', evt => positionTip(evt));
    cy.on('mouseout', 'edge', evt => {
      evt.target.removeClass('robotics-hover');
      cy.elements().removeClass('robotics-dim');
      hideTip();
    });

    cy.on('mouseover', 'node', evt => {
      const node = evt.target;
      node.addClass('robotics-hover');
      const neighborhood = node.closedNeighborhood();
      cy.elements().addClass('robotics-dim');
      neighborhood.removeClass('robotics-dim');
      if (state.callbacks.onEdgeHover) {
        try { state.callbacks.onEdgeHover(null, node); } catch (_) {}
      }
    });
    cy.on('mouseout', 'node', evt => {
      evt.target.removeClass('robotics-hover');
      cy.elements().removeClass('robotics-dim');
    });

    cy.on('tap', 'node', evt => {
      if (state.callbacks.onNodeClick) {
        try { state.callbacks.onNodeClick(evt.target.data(), evt.target); } catch (_) {}
      }
    });
    cy.on('tap', 'edge', evt => {
      if (state.callbacks.onEdgeClick) {
        try { state.callbacks.onEdgeClick(evt.target.data(), evt.target); } catch (_) {}
      }
    });
    cy.on('tap', evt => {
      if (evt.target === cy) hideTip();
    });

    // Right-click: "spread out" context menu. The page owns the menu UI;
    // this just reports what was clicked (a node, or the empty canvas).
    cy.on('cxttap', 'node', evt => {
      if (evt.originalEvent && evt.originalEvent.preventDefault) evt.originalEvent.preventDefault();
      if (state.callbacks.onNodeContextMenu) {
        try { state.callbacks.onNodeContextMenu(evt.target.data(), evt.originalEvent); } catch (_) {}
      }
    });
    cy.on('cxttap', evt => {
      if (evt.target !== cy) return;
      if (evt.originalEvent && evt.originalEvent.preventDefault) evt.originalEvent.preventDefault();
      if (state.callbacks.onBackgroundContextMenu) {
        try { state.callbacks.onBackgroundContextMenu(evt.originalEvent); } catch (_) {}
      }
    });
  }

  function renderSignature(payload, filters) {
    const f = filters || {};
    return JSON.stringify([
      payload.generated_at || '',
      f.timeWindowDays,
      f.minConfidence,
      !!f.showInvalidated,
      (f.evidenceTypes || []).slice().sort(),
      f.egoEntity || '',
      f.egoMaxEdges || 0,
    ]);
  }

  // Preset-position path: paint in degree-descending batches of CHUNK_NODES
  // per animation frame, each batch bringing along the edges whose endpoints
  // are both on screen. Cheap because no force layout runs.
  function renderChunked(nodes, edges, token) {
    const ordered = nodes.slice().sort((a, b) =>
      (b.edge_count || 0) - (a.edge_count || 0) || (b.heat || 0) - (a.heat || 0));
    const els = buildElements(ordered, edges, true);
    const nodeEls = els.filter(el => el.group === 'nodes');
    const edgeEls = els.filter(el => el.group === 'edges');

    const batchOfNode = new Map();
    nodeEls.forEach((el, i) => batchOfNode.set(el.data.id, Math.floor(i / CHUNK_NODES)));
    const batchCount = Math.max(1, Math.ceil(nodeEls.length / CHUNK_NODES));

    const batches = [];
    for (let i = 0; i < batchCount; i++) {
      batches.push(nodeEls.slice(i * CHUNK_NODES, (i + 1) * CHUNK_NODES));
    }
    edgeEls.forEach(el => {
      const b = Math.max(
        batchOfNode.get(el.data.source) ?? 0,
        batchOfNode.get(el.data.target) ?? 0);
      batches[b].push(el);
    });

    return new Promise(resolve => {
      let i = 0;
      const step = () => {
        // A newer render() (tab switch / filter change) cancels this loop.
        if (token !== state.renderToken || !state.cy) { resolve(false); return; }
        state.cy.batch(() => { state.cy.add(batches[i]); });
        if (i === 0) {
          try { state.cy.fit(undefined, 40); } catch (_) {}
        }
        i += 1;
        if (i < batches.length) {
          requestAnimationFrame(step);
        } else {
          try {
            state.cy.layout({ name: 'preset', fit: false, animate: false }).run();
            fitCore(state.cy);
          } catch (_) {}
          state.hasFitted = true;
          resolve(true);
        }
      };
      requestAnimationFrame(step);
    });
  }

  // Fit the view to the CORE of a preset-position graph (5th–95th percentile
  // box): a few far-away outliers from the full-history layout must not shrink
  // the whole picture. Outliers stay reachable by zooming out.
  function fitCore(cy) {
    const ns = cy.nodes();
    if (ns.length < 20) { cy.fit(undefined, 40); return; }
    const xs = ns.map(n => n.position('x')).sort((a, b) => a - b);
    const ys = ns.map(n => n.position('y')).sort((a, b) => a - b);
    const q = (arr, p) => arr[Math.floor(p * (arr.length - 1))];
    const [x1, x2, y1, y2] = [q(xs, 0.05), q(xs, 0.95), q(ys, 0.05), q(ys, 0.95)];
    const core = ns.filter(n => { const p = n.position(); return p.x >= x1 && p.x <= x2 && p.y >= y1 && p.y <= y2; });
    cy.fit(core.length ? core : undefined, 40);
  }

  // Nodes currently within the viewport (for an empty-canvas "spread out" —
  // declutter what's actually on screen, not the whole dataset).
  function nodesInViewport(cy) {
    const ext = cy.extent();
    return cy.nodes().filter(n => {
      const p = n.position();
      return p.x >= ext.x1 && p.x <= ext.x2 && p.y >= ext.y1 && p.y <= ext.y2;
    });
  }

  // Nodes within `pad` of a bounding box (for a node-centered "spread out" —
  // nearby unrelated nodes come along as fixed, locked obstacles so the
  // fanned-out neighborhood doesn't land on top of them). Capped to the
  // `maxCount` closest to the box's centre — an uncapped box around a hub
  // node can sweep in most of the graph, and locking hundreds of nodes
  // turns the layout's per-iteration cost (still O(n) over everything
  // locked, even though those nodes don't move) into multi-second jank.
  function nodesNear(cy, bb, pad, maxCount) {
    const x1 = bb.x1 - pad, y1 = bb.y1 - pad, x2 = bb.x2 + pad, y2 = bb.y2 + pad;
    let found = cy.nodes().filter(n => {
      const p = n.position();
      return p.x >= x1 && p.x <= x2 && p.y >= y1 && p.y <= y2;
    });
    if (maxCount && found.length > maxCount) {
      const cx = (bb.x1 + bb.x2) / 2, cy2 = (bb.y1 + bb.y2) / 2;
      const dist2 = n => { const p = n.position(); return (p.x - cx) ** 2 + (p.y - cy2) ** 2; };
      found = found.sort((a, b) => dist2(a) - dist2(b)).slice(0, maxCount);
    }
    return found;
  }

  // Fallback for payloads without positions: single-shot cose(-bilkent)
  // layout, as before — chunking a force layout just thrashes it.
  function renderSingleShot(nodes, edges) {
    const elements = buildElements(nodes, edges, false);
    state.cy.batch(() => { state.cy.add(elements); });

    const layoutName = ensureLayoutRegistered();
    const layout = state.cy.layout(layoutOpts(layoutName));
    return new Promise(resolve => {
      let settled = false;
      const done = () => { if (!settled) { settled = true; resolve(true); } };
      layout.one('layoutstop', done);
      layout.run();
      // Safety net: some layouts don't emit layoutstop when given 0 elements.
      setTimeout(done, 30000);

      if (!state.hasFitted) {
        state.hasFitted = true;
        setTimeout(() => { try { state.cy.fit(undefined, 40); } catch (_) {} }, 0);
      }
    });
  }

  const RoboticsGraph = {
    init(container, opts) {
      if (!container) throw new Error('[RoboticsGraph] container required');
      if (typeof window.cytoscape !== 'function') {
        throw new Error('[RoboticsGraph] window.cytoscape missing — include cytoscape.min.js before init');
      }
      state.container = container;
      state.callbacks = opts || {};
      state.hasFitted = false;
      state.lastSignature = null;

      container.classList.add('robotics-graph');
      if (!getComputedStyle(container).position || getComputedStyle(container).position === 'static') {
        container.style.position = 'relative';
      }

      const canvas = document.createElement('div');
      canvas.className = 'robotics-graph-canvas';
      container.appendChild(canvas);

      state.tipEl = createTip(container);
      state.legendEl = createLegend(container);

      ensureLayoutRegistered();

      state.cy = window.cytoscape({
        container: canvas,
        elements: [],
        style: stylesheet(),
        wheelSensitivity: 0.3,
        minZoom: 0.1,
        maxZoom: 3,
      });

      wireEvents(state.cy);
      createZoomControl(container, state.cy);
      return state.cy;
    },

    // Returns a Promise that settles when the paint is complete (last chunk
    // added, or layoutstop in the force fallback). A no-op cache hit or a
    // render superseded by a newer call resolves immediately.
    render(payload, filters) {
      if (!state.cy) throw new Error('[RoboticsGraph] init() must be called before render()');

      // Cache-hit check BEFORE bumping renderToken: a no-op call must not
      // cancel a real paint that's still in flight from an earlier call —
      // that paint's own completion handler checks token === renderToken
      // to decide whether to clear state.loading, and a token bump here
      // with no new paint to replace it would make that check fail forever,
      // leaving state.loading stuck true (same pattern renderProgressive()
      // already uses for its own early-return).
      if (payload && payload.graph) {
        const sig = renderSignature(payload, filters);
        if (sig === state.lastSignature && state.cy.elements().length > 0) {
          return Promise.resolve(false);
        }
      }

      state.renderToken += 1;
      const token = state.renderToken;
      stopActiveSpread();

      if (!payload || !payload.graph) {
        state.lastSignature = null;
        state.loading = false;
        state.cy.elements().remove();
        return Promise.resolve(false);
      }

      state.lastSignature = renderSignature(payload, filters);
      state.loading = true;

      hideTip();
      state.cy.elements().remove();

      const { nodes, edges } = filterPayload(payload, filters);
      const paint = (nodes.length > SMALL_LAYOUT_NODES && mostHavePositions(nodes))
        ? renderChunked(nodes, edges, token)
        : renderSingleShot(nodes, edges);
      // Only the still-current call clears loading — a superseded render's
      // own completion must not stomp on a newer one that's still going.
      // The rejection branch still re-throws (not `return r`): these paints
      // aren't expected to reject, but a resolved promise carrying an Error
      // as its value would silently swallow a real failure.
      const finishLoading = r => { if (token === state.renderToken) state.loading = false; return r; };
      const finishLoadingErr = e => { if (token === state.renderToken) state.loading = false; throw e; };
      const ego = (filters || {}).egoEntity;
      if (!ego) return paint.then(finishLoading, finishLoadingErr);
      // Highlight the searched entity once the paint settles.
      return paint.then(ok => {
        if (state.cy) {
          const n = state.cy.getElementById(ego);
          if (n && n.length) n.addClass('robotics-ego');
        }
        return ok;
      }).then(finishLoading, finishLoadingErr);
    },

    destroy() {
      state.renderToken += 1;   // cancel any in-flight chunk loop
      state.lastSignature = null;
      state.loading = false;
      stopActiveSpread();
      if (state.cy) {
        try { state.cy.destroy(); } catch (_) {}
        state.cy = null;
      }
      if (state.container) {
        state.container.querySelectorAll('.robotics-graph-canvas, .robotics-edge-tip, .robotics-graph-legend, .robotics-zoom')
          .forEach(el => el.remove());
        state.container.classList.remove('robotics-graph');
      }
      state.container = null;
      state.tipEl = null;
      state.legendEl = null;
      state.callbacks = {};
      state.hasFitted = false;
    },

    // Progressive load for big views. Elements are built ONCE for the final
    // view (global node sizing + parallel-edge fan-out, preset positions — one
    // stable coordinate system, no force layout), then added in date slices,
    // newest first (steps = window days, e.g. [7, 30, 90, 0]), a batch per
    // animation frame. The page never freezes; pause() holds the loader (what's
    // drawn stays); any newer render()/renderProgressive() cancels it via the
    // token taken at the START. Re-entering with the same final view is a
    // no-op (tab switches don't reload). onProgress({added,total,step,done}).
    renderProgressive(payload, filters, steps, onProgress) {
      if (!state.cy) throw new Error('[RoboticsGraph] init() must be called before renderProgressive()');
      const base = Object.assign({}, filters || {});
      const finalF = Object.assign({}, base, { timeWindowDays: steps[steps.length - 1] });
      const finalSig = renderSignature(payload, finalF);
      if (finalSig === state.lastSignature && state.cy.elements().length > 0) {
        const n = state.cy.edges().length;
        if (onProgress) onProgress({ added: n, total: n, step: finalF.timeWindowDays, done: true });
        return Promise.resolve(false);
      }
      state.renderToken += 1;
      const token = state.renderToken;
      stopActiveSpread();
      state.paused = false;
      state.lastSignature = null;
      state.loading = true;
      hideTip();
      state.cy.elements().remove();

      const { nodes, edges } = filterPayload(payload, finalF);
      // No usable positions (layout doc missing/failed to load) → presets would
      // pile everything at (0,0). Fall back to a normal render (force layout):
      // slower, but readable.
      if (!mostHavePositions(nodes)) {
        if (onProgress) onProgress({ added: edges.length, total: edges.length, step: finalF.timeWindowDays, done: true });
        return this.render(payload, finalF);
      }
      const els = buildElements(nodes, edges, true);
      // Slice index per edge by its catalyst date (0 = newest window).
      const gen = parseDate(payload.generated_at) || new Date();
      const cuts = steps.map(d => (d > 0 ? new Date(gen.getTime() - d * 86400000) : null));
      const sliceOfDate = s => {
        const d = parseDate(s);
        for (let i = 0; i < cuts.length; i++) if (!cuts[i] || !d || d >= cuts[i]) return i;
        return cuts.length - 1;
      };
      const slices = steps.map(() => ({ nodes: [], edges: [] }));
      const nodeSlice = new Map();
      els.filter(el => el.group === 'edges').forEach(el => {
        const i = sliceOfDate(el.data.catalystDate);
        slices[i].edges.push(el);
        [el.data.source, el.data.target].forEach(id =>
          nodeSlice.set(id, Math.min(nodeSlice.has(id) ? nodeSlice.get(id) : i, i)));
      });
      els.filter(el => el.group === 'nodes').forEach(el =>
        slices[nodeSlice.has(el.data.id) ? nodeSlice.get(el.data.id) : 0].nodes.push(el));

      const total = edges.length;
      let added = 0;
      const report = (step, done) => onProgress && onProgress({ added, total, step, done });
      const sleep = ms => new Promise(r => setTimeout(r, ms));
      const frame = () => new Promise(r => requestAnimationFrame(r));
      const live = () => token === state.renderToken && !!state.cy;

      // Only the still-current call clears loading (see finishLoading in
      // render() — same reasoning: a superseded load must not clear a newer
      // one's loading flag; the error branch rethrows rather than
      // swallowing a real failure as a resolved value).
      const finishLoading = r => { if (token === state.renderToken) state.loading = false; return r; };
      const finishLoadingErr = e => { if (token === state.renderToken) state.loading = false; throw e; };
      return (async () => {
        for (let si = 0; si < slices.length; si++) {
          const batchEls = slices[si].nodes.concat(slices[si].edges);   // nodes first
          for (let i = 0; i < batchEls.length; i += PROGRESSIVE_BATCH) {
            while (state.paused) {
              if (!live()) return false;
              await sleep(200);
            }
            if (!live()) return false;
            const chunk = batchEls.slice(i, i + PROGRESSIVE_BATCH);
            state.cy.batch(() => { state.cy.add(chunk); });
            added += chunk.filter(el => el.group === 'edges').length;
            report(steps[si], false);
            await frame();
          }
          if (si === 0 && live()) { try { fitCore(state.cy); } catch (_) {} }
        }
        if (!live()) return false;
        state.lastSignature = finalSig;                 // tab switch back → no-op
        state.hasFitted = true;
        try { fitCore(state.cy); } catch (_) {}
        report(steps[steps.length - 1], true);
        return true;
      })().then(finishLoading, finishLoadingErr);
    },

    pause() { state.paused = true; },
    resume() { state.paused = false; },
    isPaused() { return state.paused; },

    // True while a render()/renderProgressive() is actively adding/removing
    // elements. Callers (the right-click menu) should treat this as "not
    // interactive yet" rather than race a spreadOut() against it.
    isLoading() { return !!state.loading; },

    // "Spread out" — loosely re-layout an overcrowded cluster without
    // disturbing the rest of the graph: opts.nodeId fans out that node's
    // neighborhood (capped at SPREAD_MAX_NEIGHBORS, ranked by incident edge
    // confidence); no nodeId declutters whatever's currently in the
    // viewport (capped at SPREAD_MAX_BACKGROUND_NODES, ranked by edge
    // count). Nearby nodes outside the target set are locked (immovable)
    // but still part of the layout, so they act as obstacles — the moving
    // set won't settle on top of them. Positions start from where they
    // already are (randomize: false), so this nudges the clump apart
    // rather than reshuffling it. Purely cosmetic and ephemeral: it never
    // touches payload data or the exported full-history layout, and any
    // real render() / renderProgressive() call discards it exactly like it
    // would discard a manual drag.
    spreadOut(opts) {
      if (!state.cy || state.loading) return false;
      const cy = state.cy;
      const o = opts || {};
      let focal;
      if (o.nodeId) {
        const center = cy.getElementById(o.nodeId);
        if (!center || center.empty()) return false;
        let neighbors = center.closedNeighborhood().nodes().filter(n => n.id() !== o.nodeId);
        if (neighbors.length > SPREAD_MAX_NEIGHBORS) {
          const score = n => n.connectedEdges().reduce((s, e) => s + (e.data('confidence') || 0), 0);
          neighbors = neighbors.sort((a, b) => score(b) - score(a)).slice(0, SPREAD_MAX_NEIGHBORS);
        }
        focal = center.union(neighbors);
      } else {
        focal = nodesInViewport(cy);
        if (focal.length > SPREAD_MAX_BACKGROUND_NODES) {
          focal = focal.sort((a, b) => (b.data('edgeCount') || 0) - (a.data('edgeCount') || 0))
            .slice(0, SPREAD_MAX_BACKGROUND_NODES);
        }
      }
      if (focal.length < 2) return false;

      stopActiveSpread();   // a second spread-out click supersedes the first

      const bb = focal.boundingBox();
      const pad = Math.max(bb.w, bb.h, 200) * 0.6;
      const context = o.nodeId ? nodesNear(cy, bb, pad, SPREAD_MAX_OBSTACLES).union(focal) : focal;
      const movingIds = new Set(focal.map(n => n.id()));
      const toLock = context.filter(n => !movingIds.has(n.id()) && !n.locked());
      toLock.lock();
      state.spreadLockedNodes = toLock;

      const cleanup = () => {
        if (state.activeSpreadLayout !== layout) return;   // already superseded
        if (state.spreadLockedNodes) { try { state.spreadLockedNodes.unlock(); } catch (_) {} }
        state.activeSpreadLayout = null;
        state.spreadLockedNodes = null;
        state.spreading = false;
      };

      // Edges with only one endpoint in `context` get dropped, not kept as
      // a pull toward a fixed-but-excluded node: cose-bilkent (if it's ever
      // fixed to load — it doesn't today, ensureLayoutRegistered() falls
      // back to 'cose' below) throws on them via getEdgesBetween, and they
      // aren't needed since every real neighbor of a moving node is already
      // in `context` by construction.
      const eles = context.union(context.edgesWith(context));

      // Core cose ends every run with an unconditional "pack components"
      // step that translates each connected component's UNLOCKED nodes to
      // its own grid cell starting at (0,0) — ignoring randomize/gravity/
      // fit entirely. Left alone, that makes the spread jump to wherever
      // that packing lands instead of staying where the clump actually is.
      // Record each component's pre-run centroid (locked obstacles excluded
      // — they don't move, and would skew the average toward their fixed
      // positions) and shift it back by the same delta once layoutstop
      // fires, preserving the relative fan-out cose computed while undoing
      // only the part that relocates it.
      const centroidOf = nodes => {
        let sx = 0, sy = 0, n = 0;
        nodes.forEach(node => { const p = node.position(); sx += p.x; sy += p.y; n += 1; });
        return n ? { x: sx / n, y: sy / n } : null;
      };
      const components = eles.components()
        .map(comp => ({ movable: comp.nodes().filter(n => !n.locked()) }))
        .filter(c => c.movable.length > 0);
      components.forEach(c => { c.preCentroid = centroidOf(c.movable); });
      let corrected = false;
      const savedPositions = new Map();
      const undoComponentPacking = () => {
        if (corrected) {
          // A second layoutstop (the trailing animation frame after an
          // interrupting supersede) can re-pack our already-corrected
          // nodes while some of them have since been locked by a NEWER
          // spread's own obstacle set. Recomputing a centroid at that
          // point would average a mix of moved-and-not-moved nodes and
          // under-correct by roughly the locked fraction. Just restore
          // the exact positions already known to be right, for whichever
          // of our nodes are still unlocked — a now-locked one belongs to
          // the newer spread, which corrects it with its own pass.
          savedPositions.forEach((pos, node) => {
            if (!node.locked() && !node.removed()) node.position(pos);
          });
          return;
        }
        components.forEach(({ movable, preCentroid }) => {
          if (!preCentroid) return;
          const post = centroidOf(movable);
          if (!post) return;
          const dx = preCentroid.x - post.x, dy = preCentroid.y - post.y;
          if (Math.abs(dx) < 0.01 && Math.abs(dy) < 0.01) return;
          movable.forEach(node => {
            const p = node.position();
            node.position({ x: p.x + dx, y: p.y + dy });
          });
        });
        components.forEach(({ movable }) => {
          movable.forEach(node => savedPositions.set(node, { x: node.position('x'), y: node.position('y') }));
        });
        corrected = true;
      };

      // Always core 'cose', not cose-bilkent: bilkent ignores locked nodes
      // outright, which would defeat the whole "obstacles stay fixed" idea.
      const layout = eles.layout(Object.assign({}, layoutOpts('cose'), {
        randomize: false,
        fit: false,
        animate: true,
        animationDuration: 400,
        nodeRepulsion: 12000,
        idealEdgeLength: 110,
        numIter: 1500,
        gravity: 0,   // no pull toward canvas center — declutter in place, don't drift
      }));
      state.activeSpreadLayout = layout;
      state.spreading = true;
      // `on`, not `one`: cose's stop() emits layoutstop immediately AND the
      // already-queued animation frame still runs one more tick afterward,
      // which re-packs and emits layoutstop a second time — a `one` listener
      // would miss that second, trailing pack-to-origin. undoComponentPacking
      // is safe to call repeatedly (it always measures against the ORIGINAL
      // pre-run centroid, so a second call after the first already corrected
      // it just measures ~0 delta and no-ops); cleanup() is independently
      // idempotent via its own supersede check. The timeout/catch paths
      // below don't call undoComponentPacking at all — they can't be sure
      // the layout actually settled, so they only release locks.
      layout.on('layoutstop', () => { undoComponentPacking(); cleanup(); });
      // Safety net (same reasoning as renderSingleShot's 30s one below):
      // don't fully trust a 3rd-party layout to always emit layoutstop —
      // an interrupted/superseded run in particular isn't guaranteed to.
      // cleanup() is idempotent (its own supersede check no-ops a 2nd call),
      // so this is harmless if layoutstop does fire normally first.
      setTimeout(cleanup, 3000);
      try {
        layout.run();
      } catch (_) {
        cleanup();   // a layout error must still release the locks it took
      }
      return true;
    },

    instance() { return state.cy; },   // read-only access (tests / diagnostics)

    fit() {
      if (state.cy) {
        try { state.cy.fit(undefined, 40); } catch (_) {}
      }
    },

    focusNode(nodeId) {
      if (!state.cy || !nodeId) return;
      const node = state.cy.getElementById(nodeId);
      if (!node || node.empty()) return;
      state.cy.animate({
        center: { eles: node },
        zoom: 1.4,
        duration: 350,
        easing: 'ease-in-out',
      });
    },

  };

  // Exposed so the page can report exactly what the ego view keeps vs drops
  // (same filters as render — no second, drifting implementation).
  RoboticsGraph.filterPayload = filterPayload;
  window.RoboticsGraph = RoboticsGraph;
})();
