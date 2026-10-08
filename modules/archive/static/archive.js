/* Archive module front-end.
 *
 * Buttons: "Archive" in the gallery bulk bar (selection) and the viewer
 * toggles (current file); "Unarchive" / "Restore" in the bulk bar while the
 * Archive view is active. The Archive gallery view lists archived files (live
 * ones with their normal thumbnail, packed ones with the stored snapshot and a
 * "packed" badge) as gallery-item tiles, so the grid's selection and bulk bar
 * apply, with a header of counts, bytes and the Run policy / Pack now buttons. */
(function () {
  "use strict";

  const S = { host: null, body: null, head: null, ctx: { q: "", folder: "", album: "" }, gen: 0, files: [], active: false };

  const esc = s => (typeof _esc === "function") ? _esc(s) : String(s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtBytes = n => {
    n = Number(n) || 0;
    const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i ? n.toFixed(1) : n) + " " + u[i];
  };
  const toast = m => (typeof showToast === "function") ? showToast(m) : alert(m);

  /** @brief JSON POST through the core's fetch (CSRF is added by the auth layer). */
  async function post(url, body) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}) });
    return r.json().catch(() => ({ success: false, error: "HTTP " + r.status }));
  }

  function selection() {
    return [...(window.selectedFiles || [])];
  }

  /** @brief Reload whatever is showing: the archive view, else the grid. */
  function reload() {
    if (S.active) render();
    else if (typeof loadGallery === "function") loadGallery();
  }

  /** @brief Archive (or unarchive) a list of files. */
  async function setArchived(files, archived) {
    if (!files.length) { toast("Nothing selected."); return; }
    const d = await post("/api/archive/set", { filenames: files, archived });
    if (!d.success) { toast(d.error || "Archive failed."); return; }
    toast(`${archived ? "Archived" : "Unarchived"} ${d.count} file(s).`);
    if (window.selectedFiles) window.selectedFiles.clear();
    reload();
  }
  window.archiveSelected = () => setArchived(selection(), true);
  window.unarchiveSelected = () => setArchived(selection(), false);
  window.archiveCurrent = () => setArchived(window.currentFile ? [window.currentFile] : [], true);

  /** @brief Restore (unpack when packed) and unarchive the selection. */
  window.archiveRestoreSelected = async function () {
    const files = selection();
    if (!files.length) { toast("Nothing selected."); return; }
    const d = await post("/api/archive/restore", { filenames: files });
    if (!d.success) { toast(d.error || "Restore failed."); return; }
    toast(`Restored ${d.unarchived} file(s).`);
    if (window.selectedFiles) window.selectedFiles.clear();
    reload();
  };

  /** @brief Run the auto-archive policy now. */
  window.archiveRunPolicy = async function (btn) {
    if (btn) btn.disabled = true;
    try {
      const d = await post("/api/archive/policy/run", {});
      toast(d.success ? `Policy archived ${d.archived} of ${d.candidates} candidate(s).` : (d.error || "Policy failed."));
    } finally { if (btn) btn.disabled = false; }
    reload();
  };

  /** @brief Pack eligible archived files now (ignores the minimum-files threshold). */
  window.archiveRunPack = async function (btn) {
    if (btn) btn.disabled = true;
    try {
      const d = await post("/api/archive/pack/run", { force: true });
      toast(d.success ? `Packed ${d.packed} file(s), repacked ${d.repacked} pack(s).` : (d.error || "Pack failed."));
    } finally { if (btn) btn.disabled = false; }
    reload();
  };

  // -- the view ------------------------------------------------------------
  function params() {
    const p = new URLSearchParams();
    for (const k of ["q", "folder", "album"]) if (S.ctx[k]) p.set(k, S.ctx[k]);
    p.set("limit", "2000");
    return p;
  }

  function thumbUrl(f) {
    return (f.packed ? "/api/archive/thumb/" : "/api/thumb/") + encodeURIComponent(f.filename);
  }

  function tile(f) {
    const div = document.createElement("div");
    div.className = "gallery-item arc-tile relative";
    div.dataset.filename = f.filename;
    div.dataset.kind = f.kind === "video" ? "video" : "image";
    div.dataset.src = thumbUrl(f);
    div.title = f.filename + (f.reason ? ` (${f.reason})` : "") + (f.packed ? " [packed]" : "");
    div.innerHTML = `<img alt="" loading="lazy" class="w-full h-full object-cover rounded">
      ${f.packed ? '<span class="absolute bottom-1 right-1 text-[9px] px-1 rounded bg-gray-900/80 text-amber-300 font-bold uppercase">packed</span>' : ""}
      <span class="sel-check hidden absolute top-1 left-1 w-4 h-4 rounded-full bg-blue-500 border-2 border-white flex items-center justify-center text-[8px] font-bold text-white">&#10003;</span>`;
    const im = div.querySelector("img");
    im.src = div.dataset.src;
    im.classList.add("loaded");
    div.addEventListener("click", e => {
      if (f.packed) {
        // no live file to open: packed tiles only select
        if (window.selectedFiles) {
          if (window.selectedFiles.has(f.filename)) window.selectedFiles.delete(f.filename);
          else window.selectedFiles.add(f.filename);
          if (typeof refreshSelectionUI === "function") refreshSelectionUI();
        }
        return;
      }
      if (typeof handleGalleryClick === "function") handleGalleryClick(e, f.filename);
      else if (typeof selectFile === "function") selectFile(f.filename);
    });
    return div;
  }

  function header(st, total) {
    const c = st.counts || {}, b = st.bytes || {};
    const pol = st.policy || {};
    const polText = pol.archive_policy_enabled
      ? `policy on (every ${pol.archive_policy_interval_hours}h)` : "policy off";
    const packText = (st.pack && st.pack.archive_pack_enabled)
      ? `packing on (${st.pack.archive_pack_compression}, after ${st.pack.archive_pack_after_days}d)` : "packing off";
    return `<div class="flex flex-wrap items-center gap-2 text-xs text-gray-300 px-2 py-1">
      <b>${esc(total)}</b> shown, ${esc(c.archived || 0)} archived (${esc(c.packed || 0)} packed),
      live ${esc(fmtBytes(b.live))}, packs ${esc(fmtBytes(b.packs))} for ${esc(fmtBytes(b.packed_original))} of originals
      <span class="text-gray-500">| ${esc(polText)}, ${esc(packText)}</span>
      <span class="flex-1"></span>
      <span data-feature="archive">
        ${cimButton({ label: "Run policy now", onclick: "archiveRunPolicy(this)", variant: "secondary", size: "xs",
          title: "Apply the auto-archive policy (Settings > Archive) now" })}
        ${cimButton({ label: "Pack now", onclick: "archiveRunPack(this)", variant: "warn", size: "xs",
          title: "Pack every eligible archived file into the cold store now" })}
      </span>
    </div>`;
  }

  async function render() {
    if (!S.host) return;
    const gen = ++S.gen;
    S.body.innerHTML = '<div class="p-4 text-sm text-gray-400">Loading...</div>';
    try {
      const [list, st] = await Promise.all([
        fetch("/api/archive/list?" + params()).then(r => r.json()),
        fetch("/api/archive/status").then(r => r.json())]);
      if (gen !== S.gen) return;
      if (!list.success) throw new Error(list.error || "request failed");
      S.files = list.files;
      S.head.innerHTML = header(st.success ? st : {}, list.total);
      if (window.CIMFeatures && CIMFeatures.apply) CIMFeatures.apply(S.head);
      S.body.innerHTML = "";
      if (!list.files.length) {
        S.body.innerHTML = '<div class="p-4 text-sm text-gray-400">Nothing archived in this scope.</div>';
      } else {
        const grid = document.createElement("div");
        grid.className = "grid gap-2 p-2";
        grid.style.gridTemplateColumns = "repeat(auto-fill, minmax(112px, 1fr))";
        for (const f of list.files) grid.appendChild(tile(f));
        S.body.appendChild(grid);
      }
      try { galleryFiles = list.files; } catch (e) { /* gallery.js absent */ }
      if (typeof refreshSelectionUI === "function") refreshSelectionUI();
    } catch (e) {
      if (gen === S.gen) S.body.innerHTML = `<div class="p-4 text-sm text-red-400">${esc(e.message)}</div>`;
    }
  }

  const view = {
    id: "archive",
    label: "Archive",
    title: "Archive (archived and packed files)",
    feature: "archive",
    mount(host, ctx) {
      S.host = host; S.ctx = ctx || S.ctx; S.active = true;
      host.innerHTML = '<div id="arc_head"></div><div id="arc_body" class="overflow-auto" style="max-height:calc(100vh - 160px)"></div>';
      S.head = host.querySelector("#arc_head");
      S.body = host.querySelector("#arc_body");
      document.querySelectorAll(".arc-view-only").forEach(b => b.classList.remove("hidden"));
      render();
    },
    refresh(ctx) { S.ctx = ctx || S.ctx; if (S.host) render(); },
    unmount() {
      S.gen++; S.active = false; S.host = S.body = S.head = null; S.files = [];
      document.querySelectorAll(".arc-view-only").forEach(b => b.classList.add("hidden"));
      try { galleryFiles = []; } catch (e) { /* ignore */ }
    },
  };

  function register() {
    if (window.registerControlButton) {
      registerControlButton("gallery_bulk", { label: "Archive", onclick: "archiveSelected()", feature: "archive",
        variant: "neutral", title: "Hide the selected files in the archive" });
      registerControlButton("gallery_bulk", { label: "Unarchive", onclick: "unarchiveSelected()", feature: "archive",
        variant: "ok", cls: "arc-view-only hidden", title: "Put the selected archived files back in the library" });
      registerControlButton("gallery_bulk", { label: "Restore", onclick: "archiveRestoreSelected()", feature: "archive",
        variant: "warn", cls: "arc-view-only hidden", title: "Unpack (when packed) and unarchive the selected files" });
      registerControlButton("viewer_toggles", { label: "Archive", onclick: "archiveCurrent()", feature: "archive",
        variant: "neutral", title: "Archive this file" });
    }
    if (window.registerGalleryView) registerGalleryView(view);
  }
  if (document.readyState === "loading") window.addEventListener("DOMContentLoaded", register);
  else register();
})();
