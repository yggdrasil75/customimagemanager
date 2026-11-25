/* The top-right corner (#cim_corner, in app.html) belongs to the core: the account
 * badge, the notification bell and the avatar go in it (window.CIMCorner.add, or
 * the "corner" ext area), never as floating boxes of their own.
 *
 * Whatever pane runs under the corner - the controls tab bar, a centre pane's
 * header (music player, reader), a theme's header - gets its header row padded
 * clear of it (.cim-corner-clear). No pane or module has to know the corner is
 * there: the row is found by position, again whenever the layout changes. Content
 * that fills a pane to the top (a page, a picture) may run under the corner; a
 * header row, with its buttons, may not.
 *
 * Publishes --cim-corner-w / --cim-corner-h. */
(function () {
  const ROW_MAX = 3;          // a header row is at most this many corner-heights tall
  let cleared = null, queued = false;

  function corner() {
    let c = document.getElementById('cim_corner');
    if (!c && document.body) {        // pages without app.html's markup (login)
      c = document.createElement('div');
      c.id = 'cim_corner';
      document.body.appendChild(c);
    }
    return c;
  }

  /** @brief The header row under the corner: from the element there, up while the
   *  ancestor still starts within the corner's height and is row-sized. */
  function rowUnder(c, r) {
    const x = Math.max(0, r.left - 2), y = r.top + r.height / 2;
    const hit = document.elementsFromPoint(x, y)
      .find(el => !c.contains(el) && el !== document.documentElement && el !== document.body);
    if (!hit) return null;
    const limit = Math.max(r.height, 24) * ROW_MAX;
    let row = null;
    for (let el = hit; el && el !== document.body; el = el.parentElement) {
      const b = el.getBoundingClientRect();
      if (b.top > r.bottom || b.height > limit) break;
      if (b.right >= r.left) row = el;        // still reaches under the corner
    }
    return row;
  }

  function update() {
    queued = false;
    const c = corner();
    if (!c) return;
    const r = c.getBoundingClientRect();
    const w = r.width ? Math.ceil(window.innerWidth - r.left) : 0;
    const root = document.documentElement.style;
    root.setProperty('--cim-corner-w', w + 'px');
    root.setProperty('--cim-corner-h', Math.ceil(r.bottom) + 'px');
    // a cleared row is found again through its own padding, so it keeps the class
    const row = w ? rowUnder(c, r) : null;
    if (row === cleared) return;
    if (cleared) cleared.classList.remove('cim-corner-clear');
    if (row) row.classList.add('cim-corner-clear');
    cleared = row;
  }

  function schedule() {
    if (queued) return;
    queued = true;
    requestAnimationFrame(update);
  }

  /** @brief Put an element in the corner (first = at the left end). */
  function add(el, first) {
    const c = corner();
    if (!c || !el) return null;
    if (first) c.prepend(el); else c.appendChild(el);
    schedule();
    return el;
  }

  function start() {
    const c = corner();
    if (!c) return;
    window.addEventListener('resize', schedule);
    if (typeof ResizeObserver !== 'undefined') {
      const ro = new ResizeObserver(schedule);
      ro.observe(c);
      ro.observe(document.body);
    }
    // panes shown / hidden / moved: media modes, layouts, the controls toggle
    new MutationObserver(muts => {
      if (muts.some(m => !c.contains(m.target) &&
                         !(m.target === cleared && m.attributeName === 'class'))) schedule();
    }).observe(document.body, { subtree: true, childList: true,
                                attributes: true, attributeFilter: ['class', 'hidden', 'style'] });
    document.addEventListener('transitionend', schedule, true);
    schedule();
  }

  window.CIMCorner = { add, update: schedule, el: corner };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
