/* notifications.js - the header bell of the notifications module.
 *
 * A bell with an unread badge sits in the signed-in user badge
 * (#cim-user-badge); with auth off, when no badge shows up, it goes next to
 * the status line (#status_text) instead. Clicking it opens a right-aligned
 * dropdown of the newest items; clicking an item marks it read and follows
 * its link (an album item opens the album). The unread count is polled every
 * poll_seconds while the page is visible, and new arrivals raise a toast. */
(function () {
  "use strict";
  const FEATURE = 'notifications';
  const BELL_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>';
  const st = { bell: null, badge: null, panel: null, poll: 60, latest: null, unread: 0,
               timer: null, stopped: false, items: [] };

  /** @brief HTML-escape a string. */
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g,
      c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  /** @brief "5m ago" style age of an epoch-seconds timestamp. */
  function ago(t) {
    const s = Math.max(0, Date.now() / 1000 - (t || 0));
    if (s < 60) return 'just now';
    if (s < 3600) return Math.floor(s / 60) + 'm ago';
    if (s < 86400) return Math.floor(s / 3600) + 'h ago';
    if (s < 86400 * 30) return Math.floor(s / 86400) + 'd ago';
    return new Date(t * 1000).toLocaleDateString();
  }

  /** @brief JSON GET / POST against the module's API; null on any failure (403 stops polling). */
  async function api(path, body) {
    const init = body === undefined ? {} :
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
    try {
      const r = await fetch('/api/notifications' + path, init);
      if (r.status === 403 || r.status === 404) { stop(); return null; }
      const j = await r.json();
      return j && j.success ? j : null;
    } catch (_) { return null; }
  }

  /** @brief Show the unread count on the badge. */
  function setUnread(n) {
    st.unread = n || 0;
    if (!st.badge) return;
    st.badge.textContent = st.unread > 99 ? '99+' : String(st.unread);
    st.badge.hidden = st.unread <= 0;
    st.bell.title = st.unread ? st.unread + ' unread notification' + (st.unread === 1 ? '' : 's')
                              : 'Notifications';
  }

  /** @brief No access to the feature: remove the bell and stop polling. */
  function stop() {
    st.stopped = true;
    clearTimeout(st.timer);
    if (st.bell) st.bell.remove();
    if (st.panel) st.panel.remove();
  }

  /** @brief Poll the unread count; toast what arrived since the last poll. */
  async function poll() {
    clearTimeout(st.timer);
    if (st.stopped) return;
    if (!document.hidden) {
      const j = await api('/unread_count');
      if (j) {
        st.poll = Math.max(5, j.poll_seconds || 60);
        const prev = st.latest;
        st.latest = j.latest || 0;
        setUnread(j.unread);
        if (prev !== null && st.latest > prev && j.unread > 0) announce(prev);
        if (st.panel && !st.panel.hidden && st.latest !== prev) loadPanel();
      }
    }
    if (!st.stopped) st.timer = setTimeout(poll, st.poll * 1000);
  }

  /** @brief Toast the unread items newer than id `since`. */
  async function announce(since) {
    if (typeof window.showToast !== 'function') return;
    const j = await api('?unread=1&limit=10');
    const fresh = ((j && j.items) || []).filter(it => it.id > since);
    if (fresh.length === 1) window.showToast(fresh[0].title);
    else if (fresh.length > 1) window.showToast(fresh.length + ' new notifications: ' + fresh[0].title);
  }

  /** @brief Place the dropdown under the bell, right-aligned to it. */
  function position() {
    const r = st.bell.getBoundingClientRect();
    st.panel.style.top = Math.round(r.bottom + 6) + 'px';
    st.panel.style.right = Math.max(8, Math.round(window.innerWidth - r.right)) + 'px';
  }

  /** @brief Build the dropdown once. */
  function buildPanel() {
    const p = document.createElement('div');
    p.className = 'cim-notif-panel';
    p.hidden = true;
    p.setAttribute('data-feature', FEATURE);
    p.innerHTML = '<div class="cim-notif-head"><strong>Notifications</strong>' +
      '<button type="button" data-act="read">Mark all read</button>' +
      '<button type="button" data-act="clear">Clear</button></div>' +
      '<div class="cim-notif-list"></div>';
    p.querySelector('[data-act="read"]').onclick = async () => {
      const j = await api('/read', { all: true });
      if (j) setUnread(j.unread);
      loadPanel();
    };
    p.querySelector('[data-act="clear"]').onclick = async () => {
      const j = await api('/delete', { all: true });
      if (j) setUnread(j.unread);
      loadPanel();
    };
    p.querySelector('.cim-notif-list').addEventListener('click', e => {
      const el = e.target.closest('.cim-notif-item');
      if (el) openItem(st.items.find(it => String(it.id) === el.dataset.id));
    });
    document.body.appendChild(p);
    document.addEventListener('mousedown', e => {
      if (!p.hidden && !p.contains(e.target) && !st.bell.contains(e.target)) p.hidden = true;
    });
    document.addEventListener('keydown', e => { if (e.key === 'Escape') p.hidden = true; });
    window.addEventListener('resize', () => { if (!p.hidden) position(); });
    st.panel = p;
    if (window.CIMFeatures) window.CIMFeatures.apply(p);
  }

  /** @brief Fetch and render the newest items into the dropdown. */
  async function loadPanel() {
    const list = st.panel.querySelector('.cim-notif-list');
    const j = await api('?limit=50');
    if (!j) { list.innerHTML = '<div class="cim-notif-empty">Could not load notifications.</div>'; return; }
    st.items = j.items || [];
    setUnread(j.unread);
    if (!st.items.length) { list.innerHTML = '<div class="cim-notif-empty">No notifications.</div>'; return; }
    list.innerHTML = st.items.map(it =>
      '<div class="cim-notif-item lv-' + esc(it.level) + (it.read ? ' is-read' : '') +
      '" data-id="' + it.id + '" title="' + esc(new Date(it.created * 1000).toLocaleString()) + '">' +
      '<span class="cim-notif-dot"></span><div class="cim-notif-main">' +
      '<div class="cim-notif-title">' + esc(it.title) + '</div>' +
      (it.body ? '<div class="cim-notif-body">' + esc(it.body) + '</div>' : '') +
      '<div class="cim-notif-time">' + esc(ago(it.created)) + '</div></div></div>').join('');
  }

  /** @brief Mark an item read and follow it: an album opens in place, else its link. */
  async function openItem(it) {
    if (!it) return;
    if (!it.read) {
      const j = await api('/read', { ids: [it.id] });
      if (j) setUnread(j.unread);
      it.read = true;
    }
    const d = it.data || {};
    if (d.album && typeof window.openAlbumGallery === 'function') {
      st.panel.hidden = true;
      window.openAlbumGallery(d.album);
      return;
    }
    if (it.link) { location.href = it.link; return; }
    loadPanel();
  }

  /** @brief Toggle the dropdown. */
  function toggle() {
    if (!st.panel) buildPanel();
    st.panel.hidden = !st.panel.hidden;
    if (!st.panel.hidden) { position(); loadPanel(); }
  }

  /** @brief Create the bell button. */
  function makeBell() {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'cim-notif-bell';
    b.setAttribute('data-feature', FEATURE);
    b.setAttribute('aria-label', 'Notifications');
    b.innerHTML = BELL_SVG + '<span class="cim-notif-badge" hidden></span>';
    b.onclick = e => { e.stopPropagation(); toggle(); };
    st.bell = b;
    st.badge = b.querySelector('.cim-notif-badge');
    return b;
  }

  /** @brief Put the bell in the user badge, else (auth off) next to the status line. */
  function mount() {
    let tries = 0;
    const tick = () => {
      if (st.stopped || st.bell) return;
      const badge = document.getElementById('cim-user-badge');
      if (badge) {
        badge.prepend(makeBell());
      } else if (++tries < 40) {
        setTimeout(tick, 100);
        return;
      } else {
        const status = document.getElementById('status_text');
        const bell = makeBell();
        if (status && status.parentElement) {
          bell.style.marginLeft = '8px';
          status.parentElement.appendChild(bell);
        } else {
          bell.classList.add('cim-notif-floating');
          document.body.appendChild(bell);
        }
      }
      if (window.CIMFeatures) window.CIMFeatures.apply(st.bell.parentElement);
      setUnread(st.unread);
      poll();
    };
    const auth = window.CIMAuth;
    ((auth && auth.ready) || Promise.resolve()).then(tick, tick);
  }

  document.addEventListener('visibilitychange', () => { if (!document.hidden && st.bell) poll(); });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount);
  else mount();
})();
