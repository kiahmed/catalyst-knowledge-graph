// Catalyst Knowledge Graph (Robotics module) — guards.js (ticket 1.13f)
// Schema-version / empty-state / loading guards.
// Plain vanilla IIFE exposing window.RoboticsGuards. No dependencies.
//
// Integration:
//   RoboticsGuards.showLoading('Loading catalysts…');
//   fetch('cards.json').then(r => r.json()).then(p => {
//     const chk = RoboticsGuards.checkSchema(p, 1);
//     if (!chk.ok) { RoboticsGuards.showBanner(chk.message, 'error'); return; }
//     const e = RoboticsGuards.isEmpty(p);
//     if (e.cards && e.nodes) RoboticsGuards.showEmptyState(rootEl, 'no-data');
//     else if (e.cards) RoboticsGuards.showEmptyState(cardsEl, 'no-cards');
//     else if (e.nodes) RoboticsGuards.showEmptyState(graphEl, 'no-nodes');
//     RoboticsGuards.hideLoading();
//   }).catch(err => {
//     RoboticsGuards.showBanner('Failed to load cards.json: ' + err.message, 'error');
//     RoboticsGuards.hideLoading();
//   });

(function () {
  'use strict';

  // ── Internal refs ─────────────────────────────────────────────
  var BANNER_ID = 'robotics-guards-banner';
  var OVERLAY_ID = 'robotics-guards-loading';
  var SPINNER_FRAMES = ['[ / ]', '[ - ]', '[ \\ ]', '[ | ]'];
  var spinnerTimer = null;

  // ── Utility: create element with class + text ─────────────────
  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  // ── Dotted-circle SVG icon for empty states ───────────────────
  function dottedCircleSvg() {
    var svgNs = 'http://www.w3.org/2000/svg';
    var svg = document.createElementNS(svgNs, 'svg');
    svg.setAttribute('viewBox', '0 0 64 64');
    svg.setAttribute('width', '48');
    svg.setAttribute('height', '48');
    svg.setAttribute('class', 'robotics-guards-empty-icon');
    var circle = document.createElementNS(svgNs, 'circle');
    circle.setAttribute('cx', '32');
    circle.setAttribute('cy', '32');
    circle.setAttribute('r', '22');
    circle.setAttribute('fill', 'none');
    circle.setAttribute('stroke', 'currentColor');
    circle.setAttribute('stroke-width', '2');
    circle.setAttribute('stroke-dasharray', '2 6');
    circle.setAttribute('stroke-linecap', 'round');
    svg.appendChild(circle);
    return svg;
  }

  // ── Schema check ──────────────────────────────────────────────
  function checkSchema(payload, version) {
    if (!payload || typeof payload !== 'object') {
      return {
        ok: false,
        message:
          'This page expects cards.json schema_version=' + version +
          ', got <missing payload>. Refresh or redeploy the site.',
      };
    }
    var got = payload.schema_version;
    if (got === version) return { ok: true, message: '' };
    var gotLabel = (got === undefined || got === null) ? '<missing>' : String(got);
    try { console.warn('[RoboticsGuards] schema_version mismatch:', got, '(expected', version + ')'); } catch (_) {}
    return {
      ok: false,
      message:
        'This page expects cards.json schema_version=' + version +
        ', got ' + gotLabel + '. Refresh or redeploy the site.',
    };
  }

  // ── Empty check ───────────────────────────────────────────────
  function isEmpty(payload) {
    var result = { cards: true, nodes: true };
    if (payload && typeof payload === 'object') {
      if (Array.isArray(payload.cards) && payload.cards.length > 0) {
        result.cards = false;
      }
      if (
        payload.graph &&
        typeof payload.graph === 'object' &&
        Array.isArray(payload.graph.nodes) &&
        payload.graph.nodes.length > 0
      ) {
        result.nodes = false;
      }
    }
    return result;
  }

  // ── Banner ────────────────────────────────────────────────────
  function hideBanner() {
    var existing = document.getElementById(BANNER_ID);
    if (existing && existing.parentNode) existing.parentNode.removeChild(existing);
  }

  function showBanner(message, kind) {
    var k = (kind === 'warn' || kind === 'info') ? kind : 'error';
    hideBanner();
    var bar = el('div', 'robotics-guards-banner robotics-guards-banner--' + k);
    bar.id = BANNER_ID;
    bar.setAttribute('role', k === 'error' ? 'alert' : 'status');

    var icon = el('span', 'robotics-guards-banner__icon');
    icon.textContent = k === 'error' ? '!' : (k === 'warn' ? '!' : 'i');
    bar.appendChild(icon);

    var msg = el('span', 'robotics-guards-banner__msg', String(message || ''));
    bar.appendChild(msg);

    if (k !== 'error') {
      var closeBtn = el('button', 'robotics-guards-banner__close', '×');
      closeBtn.setAttribute('type', 'button');
      closeBtn.setAttribute('aria-label', 'Dismiss');
      closeBtn.addEventListener('click', hideBanner);
      bar.appendChild(closeBtn);
    }

    document.body.appendChild(bar);
  }

  // ── Empty state ───────────────────────────────────────────────
  var EMPTY_COPY = {
    'no-cards': {
      title: 'No catalysts in the current window',
      helper: 'Widen the time window or run an ingest to populate the graph.',
      cta: 'Run ingest',
    },
    'no-nodes': {
      title: 'The graph is empty',
      helper: 'Widen the time window or run an ingest to populate the graph.',
      cta: 'Run ingest',
    },
    'no-data': {
      title: 'No catalysts yet — run your first ingest.',
      helper: 'Widen the time window or run an ingest to populate the graph.',
      cta: 'Run ingest',
    },
  };

  function showEmptyState(container, variant) {
    if (!container) return;
    var copy = EMPTY_COPY[variant] || EMPTY_COPY['no-data'];

    // Clear container safely (no innerHTML).
    while (container.firstChild) container.removeChild(container.firstChild);

    var wrap = el('div', 'robotics-guards-empty');
    wrap.setAttribute('data-variant', variant || 'no-data');

    wrap.appendChild(dottedCircleSvg());
    wrap.appendChild(el('h3', 'robotics-guards-empty__title', copy.title));
    wrap.appendChild(el('p', 'robotics-guards-empty__helper', copy.helper));

    var btn = el('button', 'robotics-guards-empty__cta', copy.cta);
    btn.setAttribute('type', 'button');
    btn.addEventListener('click', function () {
      try {
        window.dispatchEvent(new CustomEvent('robotics:run-ingest', {
          detail: { variant: variant || 'no-data' },
        }));
      } catch (_) {
        // IE-era CustomEvent fallback (harmless no-op for modern browsers).
        var ev = document.createEvent('Event');
        ev.initEvent('robotics:run-ingest', true, true);
        window.dispatchEvent(ev);
      }
    });
    wrap.appendChild(btn);

    container.appendChild(wrap);
  }

  // ── Loading overlay ───────────────────────────────────────────
  function startSpinner(glyphNode) {
    stopSpinner();
    var i = 0;
    spinnerTimer = window.setInterval(function () {
      i = (i + 1) % SPINNER_FRAMES.length;
      glyphNode.textContent = SPINNER_FRAMES[i];
    }, 120);
  }

  function stopSpinner() {
    if (spinnerTimer != null) {
      window.clearInterval(spinnerTimer);
      spinnerTimer = null;
    }
  }

  function showLoading(message) {
    var existing = document.getElementById(OVERLAY_ID);
    if (existing) {
      // Just update the message if already shown.
      var existingMsg = existing.querySelector('.robotics-guards-loading__msg');
      if (existingMsg) existingMsg.textContent = message || 'Loading catalysts…';
      existing.classList.remove('robotics-guards-loading--hiding');
      existing.style.opacity = '1';
      return;
    }

    var overlay = el('div', 'robotics-guards-loading');
    overlay.id = OVERLAY_ID;
    overlay.setAttribute('role', 'status');
    overlay.setAttribute('aria-live', 'polite');
    overlay.style.opacity = '0';

    var inner = el('div', 'robotics-guards-loading__inner');
    var glyph = el('div', 'robotics-guards-loading__glyph', SPINNER_FRAMES[0]);
    var msg = el('div', 'robotics-guards-loading__msg', message || 'Loading catalysts…');
    inner.appendChild(glyph);
    inner.appendChild(msg);
    overlay.appendChild(inner);

    document.body.appendChild(overlay);
    startSpinner(glyph);

    // 1-frame delay → triggers CSS transition from opacity:0 to 1.
    window.requestAnimationFrame(function () {
      window.requestAnimationFrame(function () {
        overlay.style.opacity = '1';
      });
    });
  }

  function hideLoading() {
    var overlay = document.getElementById(OVERLAY_ID);
    if (!overlay) return;
    overlay.classList.add('robotics-guards-loading--hiding');
    overlay.style.opacity = '0';
    window.setTimeout(function () {
      stopSpinner();
      if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
    }, 220);
  }

  // ── Busy cursor (reference-counted) ───────────────────────────
  // withBusy(promiseOrFn) adds `is-busy` to <body> (guards.css turns the
  // cursor into `progress`) until the promise settles. Ref-counted so
  // overlapping async ops don't clear each other's cursor early.
  var busyCount = 0;

  function busyStart() {
    busyCount += 1;
    document.body.classList.add('is-busy');
  }

  function busyEnd() {
    busyCount = Math.max(0, busyCount - 1);
    if (busyCount === 0) document.body.classList.remove('is-busy');
  }

  function withBusy(promiseOrFn) {
    busyStart();
    var p;
    try {
      p = (typeof promiseOrFn === 'function') ? promiseOrFn() : promiseOrFn;
    } catch (err) {
      busyEnd();
      throw err;
    }
    if (!p || typeof p.then !== 'function') {
      busyEnd();
      return Promise.resolve(p);
    }
    return Promise.resolve(p).then(
      function (v) { busyEnd(); return v; },
      function (err) { busyEnd(); throw err; }
    );
  }

  // ── Expose global ─────────────────────────────────────────────
  window.RoboticsGuards = {
    checkSchema: checkSchema,
    isEmpty: isEmpty,
    showBanner: showBanner,
    hideBanner: hideBanner,
    showEmptyState: showEmptyState,
    showLoading: showLoading,
    hideLoading: hideLoading,
    withBusy: withBusy,
  };
})();
