// ── Scroll preservation for list panes ──────────────────────────────────────
// The Faces and Review panes rebuild their whole list via innerHTML on every
// action (name, deny, split, select…). Replacing the children resets the
// container's scrollTop to 0, so any edit below the fold threw the user back to
// the top. These helpers snapshot the scroll offset around a rebuild and
// restore it after layout settles.

function keepScroll(id, fn) {
  const el = document.getElementById(id);
  if (!el) return fn();
  const top = el.scrollTop;
  const done = () => {
    // Restore after the browser has laid out the new content. Two rAFs because
    // the first fires before the new nodes have been measured, and the height
    // may still be growing (thumbnails are background-images, so no reflow race
    // beyond layout itself).
    requestAnimationFrame(() => requestAnimationFrame(() => {
      el.scrollTop = Math.min(top, Math.max(0, el.scrollHeight - el.clientHeight));
    }));
  };
  const r = fn();
  if (r && typeof r.then === 'function') { r.then(done, done); return r; }
  done();
  return r;
}

// Re-render a single element in place without touching the rest of the list.
// Used when an action only changes one cluster/group.
function replaceNode(el, html) {
  if (!el) return null;
  const tmp = document.createElement('div');
  tmp.innerHTML = html.trim();
  const next = tmp.firstElementChild;
  if (next) el.replaceWith(next);
  return next;
}