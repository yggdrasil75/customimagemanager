/* Trash bin gallery view (modules/trash).
 *
 * A "Trash" button in the gallery's view switcher lists what was deleted:
 * one tile per item (stored thumbnail, name, deleted date, size) with
 * Restore / Delete forever, a top bar with the totals and the bulk actions
 * (restore selected, delete selected forever, empty trash), and simple
 * multi-select (click toggles, shift selects a range). It also adds a
 * "Delete forever" button to the gallery bulk bar that bypasses the bin.
 */
(function () {
  "use strict";

  const S = { host: null, grid: null, bar: null, items: [], sel: new Set(), last: -1, gen: 0 };

  const esc = s => (typeof _esc === "function") ? _esc(String(s)) :
    String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const toast = m => (typeof showToast === "function") ? showToast(m) : alert(m);

  /** @brief Bytes as a short human string. */
  function fmtBytes(n) {
    n = Number(n) || 0;
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i ? n.toFixed(1) : n) + " " + u[i];
  }

  /** @brief Epoch seconds as a local date-time string. */
  function fmtDate(t) {
    if (!t) return "";
    return new Date(t * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  }

  async function postJSON(url, body) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                                 body: JSON.stringify(body || {}) });
    const d = await r.json().catch(() => ({ success: false, error: "HTTP " + r.status }));
    if (!r.ok || d.success === false) throw new Error(d.error || ("HTTP " + r.status));
    return d;
  }

  // -- chrome --------------------------------------------------------------
  function buildChrome(host) {
    host.innerHTML =
      '<div class="trash-bar">' +
        '<span class="trash-totals" id="trash_totals"></span>' +
        '<span class="trash-sel" id="trash_sel"></span>' +
        '<span class="flex-1"></span>' +
        cimButton({ label: "Select all", onclick: "trashSelectAll()", variant: "neutral", size: "xs" }) +
        cimButton({ label: "Restore selected", onclick: "trashRestoreSelected()", variant: "ok", size: "xs",
                    feature: "trash", cls: "trash-needs-sel" }) +
        cimButton({ label: "Delete selected forever", onclick: "trashPurgeSelected()", variant: "danger", size: "xs",
                    feature: "trash", cls: "trash-needs-sel" }) +
        cimButton({ label: "Empty trash", onclick: "trashEmpty()", variant: "danger", size: "xs", feature: "trash" }) +
      '</div>' +
      '<div class="trash-grid" id="trash_grid"></div>';
    S.grid = host.querySelector("#trash_grid");
    S.bar = host.querySelector(".trash-bar");
    if (window.CIMFeatures && CIMFeatures.apply) CIMFeatures.apply(host);
  }

  function syncBar() {
    if (!S.host) return;
    const t = S.host.querySelector("#trash_totals");
    const s = S.host.querySelector("#trash_sel");
    if (t) t.textContent = S.total + " item" + (S.total === 1 ? "" : "s") + ", " + fmtBytes(S.bytes) +
      (S.backend === "os" ? " (OS recycle bin)" : "") +
      (S.retention ? " - kept " + S.retention + " days" : "");
    if (s) s.textContent = S.sel.size ? S.sel.size + " selected" : "";
    S.host.querySelectorAll(".trash-needs-sel").forEach(b => { b.disabled = !S.sel.size; });
    S.grid?.querySelectorAll(".trash-tile").forEach(el => el.classList.toggle("on", S.sel.has(el.dataset.id)));
  }

  // -- tiles ---------------------------------------------------------------
  function tile(it, i) {
    const d = document.createElement("div");
    d.className = "trash-tile" + (S.sel.has(it.id) ? " on" : "");
    d.dataset.id = it.id;
    d.title = it.rel_path + "\nDeleted " + fmtDate(it.deleted_at) + (it.deleted_by ? " by " + it.deleted_by : "");
    d.innerHTML =
      '<div class="trash-thumb"><img loading="lazy" src="/api/trash/thumb/' + encodeURIComponent(it.id) + '" ' +
        'alt="" onerror="this.replaceWith(Object.assign(document.createElement(\'span\'),{className:\'trash-nothumb\',textContent:\'no preview\'}))"></div>' +
      '<div class="trash-name">' + esc(it.name) + '</div>' +
      '<div class="trash-meta">' + esc(fmtDate(it.deleted_at)) + ' - ' + esc(fmtBytes(it.size)) +
        (it.restorable ? "" : ' - <i>OS bin</i>') + '</div>' +
      '<div class="trash-actions">' +
        (it.restorable ? cimButton({ label: "Restore", onclick: "trashRestore('" + esc(it.id) + "')", variant: "ok", size: "xs", feature: "trash" }) : "") +
        cimButton({ label: "Delete forever", onclick: "trashPurge('" + esc(it.id) + "')", variant: "danger", size: "xs", feature: "trash" }) +
      '</div>';
    d.addEventListener("click", e => {
      if (e.target.closest("button")) return;
      if (e.shiftKey && S.last >= 0) {
        const lo = Math.min(S.last, i), hi = Math.max(S.last, i);
        for (let k = lo; k <= hi; k++) S.sel.add(S.items[k].id);
      } else if (e.ctrlKey || e.metaKey) {
        if (S.sel.has(it.id)) S.sel.delete(it.id); else S.sel.add(it.id);
      } else {
        if (S.sel.has(it.id) && S.sel.size === 1) S.sel.clear(); else { S.sel.clear(); S.sel.add(it.id); }
      }
      S.last = i;
      syncBar();
    });
    return d;
  }

  async function render() {
    if (!S.host) return;
    const gen = ++S.gen;
    S.grid.innerHTML = '<div class="trash-empty">Loading...</div>';
    try {
      const [list, st] = await Promise.all([
        fetch("/api/trash/list?limit=500").then(r => r.json()),
        fetch("/api/trash/status").then(r => r.json()),
      ]);
      if (gen !== S.gen) return;
      if (!list.success) throw new Error(list.error || "list failed");
      S.items = list.items || [];
      S.total = list.total || 0; S.bytes = list.bytes || 0;
      S.backend = st.backend; S.retention = st.retention_days;
      const keep = new Set(S.items.map(x => x.id));
      for (const id of [...S.sel]) if (!keep.has(id)) S.sel.delete(id);
      S.grid.innerHTML = "";
      if (!S.items.length) S.grid.innerHTML = '<div class="trash-empty">The trash is empty.</div>';
      S.items.forEach((it, i) => S.grid.appendChild(tile(it, i)));
      if (window.CIMFeatures && CIMFeatures.apply) CIMFeatures.apply(S.grid);
      syncBar();
    } catch (e) {
      if (gen === S.gen) S.grid.innerHTML = '<div class="trash-empty">' + esc(e.message) + '</div>';
    }
  }

  // -- actions -------------------------------------------------------------
  async function restore(ids) {
    if (!ids.length) return;
    try {
      const d = await postJSON("/api/trash/restore", { ids });
      const n = d.restored.length;
      toast(n + " restored" + (d.errors.length ? ", " + d.errors.length + " failed: " + d.errors[0].error : "."));
      ids.forEach(id => S.sel.delete(id));
      render();
    } catch (e) { toast("Restore failed: " + e.message); }
  }

  async function purge(ids) {
    if (!ids.length) return;
    if (!confirm("Delete " + ids.length + " item" + (ids.length === 1 ? "" : "s") + " forever? This cannot be undone.")) return;
    try {
      const d = await postJSON("/api/trash/purge", { ids });
      toast(d.purged + " deleted forever.");
      ids.forEach(id => S.sel.delete(id));
      render();
    } catch (e) { toast("Delete failed: " + e.message); }
  }

  window.trashRestore = id => restore([id]);
  window.trashPurge = id => purge([id]);
  window.trashRestoreSelected = () => restore([...S.sel]);
  window.trashPurgeSelected = () => purge([...S.sel]);
  window.trashSelectAll = () => {
    if (S.sel.size === S.items.length) S.sel.clear(); else S.items.forEach(x => S.sel.add(x.id));
    syncBar();
  };
  window.trashEmpty = async () => {
    if (!S.total) return;
    if (!confirm("Empty the trash? All " + S.total + " item(s) are deleted forever.")) return;
    try {
      const d = await postJSON("/api/trash/empty", {});
      toast(d.purged + " deleted forever.");
      S.sel.clear();
      render();
    } catch (e) { toast("Empty failed: " + e.message); }
  };

  /** @brief Gallery bulk bar: delete the selected files without passing through the bin. */
  window.trashDeleteForever = async () => {
    const files = [...(window.selectedFiles || [])];
    if (!files.length) return toast("Nothing selected.");
    if (!confirm("Delete " + files.length + " file" + (files.length === 1 ? "" : "s") + " forever, skipping the trash bin?")) return;
    try {
      const d = await postJSON("/api/bulk_delete", { filenames: files, permanent: true });
      toast(d.deleted + " deleted forever" + (d.errors.length ? ", " + d.errors.length + " failed." : "."));
      if (window.selectedFiles) selectedFiles.clear();
      if (typeof loadGallery === "function") loadGallery();
    } catch (e) { toast("Delete failed: " + e.message); }
  };

  // -- view contract ---------------------------------------------------------
  const view = {
    id: "trash",
    label: "Trash",
    title: "Trash bin: deleted files you can restore",
    feature: "trash",
    mount(host) {
      S.host = host;
      S.sel.clear(); S.last = -1;
      buildChrome(host);
      render();
    },
    refresh() { if (S.host) render(); },
    unmount() {
      S.gen++;
      S.host = S.grid = S.bar = null;
      S.items = []; S.sel.clear();
    },
  };

  function register() {
    if (window.registerGalleryView) registerGalleryView(view);
    if (window.registerControlButton)
      registerControlButton("gallery_bulk", { label: "Delete forever", onclick: "trashDeleteForever()",
        variant: "danger", feature: "data.delete", title: "Delete the selected files without passing through the trash bin" });
  }
  if (document.readyState === "loading") window.addEventListener("DOMContentLoaded", register);
  else register();
})();
