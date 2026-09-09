/* Pane resizing via drag splitters.
 *
 * Two resizable panes:
 *   - #left_pane   : the search/gallery column (resize width, drag #left_splitter)
 *   - #controls_pane: the editor controls (resize width in vertical layout or
 *                     height in horizontal layout, drag #controls_splitter)
 *
 * The controls splitter respects the current layout mode: #editor_region has
 * class 'vertical' (side-by-side -> resize width) or 'horizontal' (stacked ->
 * resize height). Sizes persist in localStorage and are re-applied on load.
 *
 * This replaces the old CSS `resize: horizontal` hack so BOTH panes are
 * draggable from a real handle, and so a right-docked pane resizes correctly
 * (CSS resize only pulls from the bottom-right corner). */
(function () {
  const LS = window.localStorage;
  const KEY_LEFT = "cim.pane.left.width";
  const KEY_CTRL_W = "cim.pane.controls.width";
  const KEY_CTRL_H = "cim.pane.controls.height";

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

  // ── left column: width ──────────────────────────────────────────────────
  function initLeft() {
    const pane = document.getElementById("left_pane");
    const grip = document.getElementById("left_splitter");
    if (!pane || !grip) return;
    const saved = LS && LS.getItem(KEY_LEFT);
    if (saved) pane.style.width = saved;

    grip.addEventListener("mousedown", (e) => {
      e.preventDefault();
      const startX = e.clientX, startW = pane.getBoundingClientRect().width;
      grip.classList.add("dragging");
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
      function move(ev) {
        const w = clamp(startW + (ev.clientX - startX), 300, window.innerWidth * 0.6);
        pane.style.width = w + "px";
      }
      function up() {
        document.removeEventListener("mousemove", move);
        document.removeEventListener("mouseup", up);
        grip.classList.remove("dragging");
        document.body.style.cursor = ""; document.body.style.userSelect = "";
        if (LS) LS.setItem(KEY_LEFT, pane.style.width);
        window.dispatchEvent(new Event("resize")); // let the viewer re-fit
      }
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
    });
  }

  // ── controls pane: width (vertical) or height (horizontal) ────────────────
  function initControls() {
    const region = document.getElementById("editor_region");
    const pane = document.getElementById("controls_pane");
    const grip = document.getElementById("controls_splitter");
    if (!region || !pane || !grip) return;

    function applySaved() {
      const vertical = region.classList.contains("vertical");
      if (vertical) {
        const w = LS && LS.getItem(KEY_CTRL_W);
        if (w) { pane.style.width = w; pane.style.height = ""; }
      } else {
        const h = LS && LS.getItem(KEY_CTRL_H);
        if (h) { pane.style.height = h; pane.style.width = ""; }
      }
    }
    applySaved();

    grip.addEventListener("mousedown", (e) => {
      e.preventDefault();
      const vertical = region.classList.contains("vertical");
      const rect = pane.getBoundingClientRect();
      const startX = e.clientX, startY = e.clientY;
      const startW = rect.width, startH = rect.height;
      grip.classList.add("dragging");
      document.body.style.cursor = vertical ? "col-resize" : "row-resize";
      document.body.style.userSelect = "none";
      function move(ev) {
        if (vertical) {
          // pane is docked on the RIGHT: dragging left grows it.
          const w = clamp(startW - (ev.clientX - startX), 300, window.innerWidth * 0.6);
          pane.style.width = w + "px";
        } else {
          // pane is docked at the BOTTOM: dragging up grows it.
          const h = clamp(startH - (ev.clientY - startY), 150, window.innerHeight * 0.7);
          pane.style.height = h + "px";
        }
      }
      function up() {
        document.removeEventListener("mousemove", move);
        document.removeEventListener("mouseup", up);
        grip.classList.remove("dragging");
        document.body.style.cursor = ""; document.body.style.userSelect = "";
        if (LS) {
          if (vertical) LS.setItem(KEY_CTRL_W, pane.style.width);
          else LS.setItem(KEY_CTRL_H, pane.style.height);
        }
        window.dispatchEvent(new Event("resize"));
      }
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
    });

    // Re-apply the right saved size whenever the layout mode flips.
    const mo = new MutationObserver(applySaved);
    mo.observe(region, { attributes: true, attributeFilter: ["class"] });
  }

  function init() { initLeft(); initControls(); }
  if (document.readyState === "loading")
    window.addEventListener("DOMContentLoaded", init);
  else init();
})();
