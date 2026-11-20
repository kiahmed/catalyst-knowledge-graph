// Phone-only behaviour (pairs with mobile.css). Everything here is inert unless
// <html> has .is-mobile, set by the inline detector in index.html <head>.
(function () {
  "use strict";
  const isMobile = () => document.documentElement.classList.contains("is-mobile");

  // Graph view: "Filters" / "Legend" toggles. On a phone those panels would
  // cover the map, so they stay hidden until tapped. The bar is display:none
  // outside .is-mobile (mobile.css), so desktop never shows it.
  function initGraphTools() {
    const gv = document.getElementById("graphView");
    if (!gv || document.getElementById("mGraphTools")) return;
    const bar = document.createElement("div");
    bar.id = "mGraphTools";
    bar.className = "m-graph-tools";
    bar.innerHTML =
      '<button type="button" data-panel="filters" aria-pressed="false">Filters</button>' +
      '<button type="button" data-panel="legend" aria-pressed="false">Legend</button>';
    bar.addEventListener("click", (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;
      const cls = `m-${btn.dataset.panel}-open`;
      const turnOn = !gv.classList.contains(cls);
      gv.classList.remove("m-filters-open", "m-legend-open");
      if (turnOn) gv.classList.add(cls);
      bar.querySelectorAll("button").forEach((b) => {
        const on = gv.classList.contains(`m-${b.dataset.panel}-open`);
        b.classList.toggle("active", on);
        b.setAttribute("aria-pressed", on ? "true" : "false");
      });
    });
    gv.appendChild(bar);
  }

  // Cards view: swipe left/right on the card stack to change page
  // (reuses the existing arrow buttons, so paging logic stays in one place).
  // The listeners are always attached (passive, never preventDefault), and both
  // handlers return immediately unless <html> is .is-mobile. Checking at run
  // time keeps rotation / resize across the breakpoint correct without re-binding.
  function initSwipe() {
    const stage = document.getElementById("gridStage");
    if (!stage) return;
    let x0 = null, y0 = null;
    stage.addEventListener("touchstart", (e) => {
      if (!isMobile() || e.touches.length !== 1) { x0 = null; return; }
      x0 = e.touches[0].clientX;
      y0 = e.touches[0].clientY;
    }, { passive: true });
    stage.addEventListener("touchend", (e) => {
      if (x0 === null || !isMobile()) { x0 = null; return; }
      const t = e.changedTouches[0];
      const dx = t.clientX - x0, dy = t.clientY - y0;
      x0 = null;
      if (Math.abs(dx) > 60 && Math.abs(dx) > 1.5 * Math.abs(dy)) {
        const target = document.getElementById(dx < 0 ? "navRight" : "navLeft");
        if (target && !target.classList.contains("disabled")) target.click();
      }
    }, { passive: true });
  }

  function init() { initGraphTools(); initSwipe(); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
