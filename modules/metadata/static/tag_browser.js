/** @file tag_browser.js
 *  @brief "Tags" gallery view: the tag tree of the gallery's current search / folder /
 *  album (GET /api/tags/tree) as a collapsible tree with a file count per node.
 *  Clicking a node searches `tagpath:<node>` in the grid; tags without a hierarchy
 *  sit in an "Untagged hierarchy" group. With write on annot.tags a node can be
 *  renamed or moved (POST /api/tags/rename), which rewrites every file under it.
 */
(function () {
  "use strict";

  const S = { host: null, body: null, filter: null, count: null, ctx: { q: "", folder: "", album: "" },
              tree: [], open: new Set(), gen: 0, poll: null };
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const toast = m => (typeof showToast === "function") ? showToast(m) : console.log(m);

  /** @brief The search with every tagpath: token replaced by one for `path`. */
  function searchFor(path) {
    const tok = /[\s"]/.test(path) ? `tagpath:"${path.replace(/"/g, "")}"` : `tagpath:${path}`;
    const prev = (S.ctx.q || "").match(/(?:[^\s"]+|"[^"]*"?)+/g) || [];
    return prev.filter(t => !/^-?tagpath:/i.test(t)).concat(tok).join(" ");
  }

  /** @brief Run a tagpath: search for a node in the grid. */
  function openNode(path) {
    const q = searchFor(path);
    const si = document.getElementById("search_input");
    if (si) si.value = q;
    try { currentSearch = q; currentPage = 0; } catch (e) { /* globals.js not loaded */ }
    if (typeof setGalleryView === "function") setGalleryView("grid");
    else if (typeof loadGallery === "function") loadGallery();
  }

  /** @brief Does a node or anything below it match the name filter? */
  function matches(n, f) {
    return !f || n.name.toLowerCase().includes(f) || n.children.some(c => matches(c, f));
  }

  /** @brief One node (and, when open, its children) as HTML. */
  function nodeHtml(n, f, depth) {
    if (!matches(n, f)) return "";
    const kids = n.children.filter(c => matches(c, f));
    const open = (f && kids.length) || S.open.has(n.path.toLowerCase());
    const caret = n.children.length
      ? `<button type="button" class="tb-caret" data-tb-toggle="${esc(n.path)}" title="${open ? "Collapse" : "Expand"}">${open ? "&#9662;" : "&#9656;"}</button>`
      : '<span class="tb-caret"></span>';
    return `<li class="tb-node">
      <div class="tb-row" style="padding-left:${depth * 14}px">
        ${caret}
        <button type="button" class="tb-name" data-tb-open="${esc(n.path)}" title="Show files tagged ${esc(n.path)} or below">${esc(n.name)}</button>
        <span class="tb-count">${Number(n.count).toLocaleString()}</span>
        <span class="tb-actions" data-write-gate="annot.tags">
          <button type="button" class="tb-rename" data-tb-rename="${esc(n.path)}" title="Rename or move this tag (rewrites every file under it)">&#9998;</button>
        </span>
      </div>
      ${open && kids.length ? `<ul class="tb-list">${kids.map(c => nodeHtml(c, f, depth + 1)).join("")}</ul>` : ""}
    </li>`;
  }

  /** @brief Render the tree from S.tree and the name filter. */
  function render() {
    if (!S.body) return;
    const f = (S.filter && S.filter.value || "").trim().toLowerCase();
    const roots = S.tree.filter(n => n.children.length);
    const flat = S.tree.filter(n => !n.children.length);
    let html = roots.map(n => nodeHtml(n, f, 0)).join("");
    const flatHtml = flat.map(n => nodeHtml(n, f, 1)).join("");
    if (flatHtml) {
      const open = f || S.open.has("");
      html += `<li class="tb-node tb-flat">
        <div class="tb-row">
          <button type="button" class="tb-caret" data-tb-toggle="">${open ? "&#9662;" : "&#9656;"}</button>
          <span class="tb-group" title="Tags that are not part of any path">Untagged hierarchy</span>
          <span class="tb-count">${flat.length.toLocaleString()} tag${flat.length === 1 ? "" : "s"}</span>
        </div>
        ${open ? `<ul class="tb-list">${flatHtml}</ul>` : ""}
      </li>`;
    }
    S.body.innerHTML = html ? `<ul class="tb-list tb-root">${html}</ul>`
      : `<div class="tb-empty">${S.tree.length ? "No tag matches the filter." : "No tags in this scope."}</div>`;
    if (window.CIMFeatures) window.CIMFeatures.apply(S.body);
  }

  /** @brief Fetch the tree for the current scope. */
  async function load() {
    if (!S.body) return;
    const gen = ++S.gen;
    const p = new URLSearchParams();
    for (const k of ["q", "folder", "album"]) if (S.ctx[k]) p.set(k, S.ctx[k]);
    S.body.innerHTML = '<div class="tb-empty">Loading tags...</div>';
    try {
      const r = await fetch("/api/tags/tree?" + p);
      const d = await r.json().catch(() => ({ success: false, error: "HTTP " + r.status }));
      if (gen !== S.gen) return;
      if (!d.success) throw new Error(d.error || "request failed");
      S.tree = d.tree || [];
      if (S.count) S.count.textContent = `${Number(d.files || 0).toLocaleString()} tagged file${d.files === 1 ? "" : "s"}`;
      render();
    } catch (e) {
      if (gen === S.gen && S.body) S.body.innerHTML = `<div class="tb-empty">${esc(e.message)}</div>`;
    }
  }

  /** @brief Follow a background rename until it finishes, then reload. */
  function pollRename() {
    clearTimeout(S.poll);
    S.poll = setTimeout(async () => {
      try {
        const d = await fetch("/api/tags/rename/status").then(r => r.json());
        if (d.running) {
          if (S.count) S.count.textContent = `Renaming ${d.done}/${d.total}...`;
          pollRename();
          return;
        }
        toast(`Renamed in ${d.changed} file(s)` + (d.error ? ` (last error: ${d.error})` : ""));
      } catch (e) { /* keep the tree as it is */ }
      load();
    }, 1000);
  }

  /** @brief Ask for a new path for a node and rename it. */
  async function rename(path) {
    const to = window.prompt(`Rename or move "${path}" to (use / between levels):`, path);
    if (to === null || !to.trim() || to.trim() === path) return;
    const r = await fetch("/api/tags/rename", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ from: path, to: to.trim() }) });
    const d = await r.json().catch(() => ({ success: false, error: "HTTP " + r.status }));
    if (!d.success && !d.job) { toast("Rename failed: " + (d.error || "error")); return; }
    if (d.job) { toast(`Renaming in ${d.total} files in the background...`); pollRename(); return; }
    toast(`Renamed in ${d.changed} file(s)`);
    load();
  }

  function onClick(e) {
    const t = e.target.closest("[data-tb-toggle],[data-tb-open],[data-tb-rename]");
    if (!t) return;
    if (t.dataset.tbToggle !== undefined) {
      const k = t.dataset.tbToggle.toLowerCase();
      if (S.open.has(k)) S.open.delete(k); else S.open.add(k);
      render();
    } else if (t.dataset.tbOpen !== undefined) openNode(t.dataset.tbOpen);
    else if (t.dataset.tbRename !== undefined) rename(t.dataset.tbRename);
  }

  const view = {
    id: "tags",
    label: "Tags",
    title: "Tag tree (hierarchical tags with counts)",
    feature: "annot.tags",
    mount(host, ctx) {
      S.host = host;
      S.ctx = ctx || S.ctx;
      host.innerHTML = `
        <div class="tb-bar">
          <input type="search" class="tb-filter" placeholder="Filter tags" aria-label="Filter tags">
          <span class="tb-total"></span>
        </div>
        <div class="tb-body"></div>`;
      S.body = host.querySelector(".tb-body");
      S.filter = host.querySelector(".tb-filter");
      S.count = host.querySelector(".tb-total");
      S.filter.addEventListener("input", render);
      S.body.addEventListener("click", onClick);
      load();
    },
    refresh(ctx) {
      S.ctx = ctx || S.ctx;
      load();
    },
    unmount() {
      S.gen++;
      clearTimeout(S.poll);
      S.host = S.body = S.filter = S.count = null;
    },
  };

  function register() {
    if (window.registerGalleryView) registerGalleryView(view);
  }
  if (document.readyState === "loading") window.addEventListener("DOMContentLoaded", register);
  else register();
})();
