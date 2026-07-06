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

  const state = {
    cy: null,
    container: null,
    tipEl: null,
    legendEl: null,
    callbacks: {},
    hasFitted: false,
    bilkentRegistered: false,
  };

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
    const timeWindowDays = f.timeWindowDays == null ? 90 : f.timeWindowDays;
    const minConfidence = f.minConfidence == null ? 0 : f.minConfidence;
    const showInvalidated = !!f.showInvalidated;
    const evidenceTypes = new Set(f.evidenceTypes || ['direct', 'inferred', 'web_grounded', 'speculative']);

    const today = parseDate(payload.generated_at) || new Date('2026-04-18');
    const cutoff = timeWindowDays > 0 ? new Date(today.getTime() - timeWindowDays * 86400000) : null;

    const rawEdges = (payload.graph && payload.graph.edges) || [];
    const edges = rawEdges.filter(e => {
      if (!showInvalidated && e.status === 'invalidated') return false;
      if ((e.confidence || 0) < minConfidence) return false;
      if (!evidenceTypes.has(e.evidence_type)) return false;
      if (cutoff) {
        const d = parseDate(e.catalyst_date);
        if (d && d < cutoff) return false;
      }
      return true;
    });

    const keep = new Set();
    edges.forEach(e => { keep.add(e.from); keep.add(e.to); });
    const rawNodes = (payload.graph && payload.graph.nodes) || [];
    const nodes = rawNodes.filter(n => keep.has(n.id));

    return { nodes, edges };
  }

  function buildElements(nodes, edges) {
    const maxEdgeCount = nodes.reduce((m, n) => Math.max(m, n.edge_count || 0), 1);
    const denom = Math.max(1, maxEdgeCount - 1);

    const els = [];
    nodes.forEach(n => {
      const ec = Math.max(1, n.edge_count || 1);
      const t = Math.min(1, (ec - 1) / denom);
      const size = lerp(NODE_MIN_PX, NODE_MAX_PX, t);
      const label = n.ticker ? `${n.name} (${n.ticker})` : n.name;
      els.push({
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
      });
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
          id: `e${idx}-${e.from}-${e.to}-${e.catalyst_id || idx}`,
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
        minZoom: 0.2,
        maxZoom: 3,
      });

      wireEvents(state.cy);
      return state.cy;
    },

    render(payload, filters) {
      if (!state.cy) throw new Error('[RoboticsGraph] init() must be called before render()');
      if (!payload || !payload.graph) {
        state.cy.elements().remove();
        return;
      }
      const { nodes, edges } = filterPayload(payload, filters);
      const elements = buildElements(nodes, edges);

      state.cy.batch(() => {
        state.cy.elements().remove();
        state.cy.add(elements);
      });

      const layoutName = ensureLayoutRegistered();
      const layout = state.cy.layout(layoutOpts(layoutName));
      layout.run();

      if (!state.hasFitted) {
        state.hasFitted = true;
        setTimeout(() => { try { state.cy.fit(undefined, 40); } catch (_) {} }, 0);
      }
    },

    destroy() {
      if (state.cy) {
        try { state.cy.destroy(); } catch (_) {}
        state.cy = null;
      }
      if (state.container) {
        state.container.querySelectorAll('.robotics-graph-canvas, .robotics-edge-tip, .robotics-graph-legend')
          .forEach(el => el.remove());
        state.container.classList.remove('robotics-graph');
      }
      state.container = null;
      state.tipEl = null;
      state.legendEl = null;
      state.callbacks = {};
      state.hasFitted = false;
    },

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

  window.RoboticsGraph = RoboticsGraph;
})();
