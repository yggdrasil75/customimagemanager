/* Stacks module front-end.
 *
 * - marks a stack's cover tile in the grid (layered card + member count) through
 *   the core gallery tile hook; the count badge opens the stack;
 * - the stack window: member tiles (shared selection with the grid), set cover,
 *   remove, unstack, merge into an animation, raw downloads;
 * - "Stack selected" in the gallery bulk bar, "Rescan stacks" in the review pane;
 * - viewer buttons: open the current file's stack, split an animation into a stack.
 */
(function () {
  const FEATURE = "stacks";
  const KIND_LABEL = { raw: "Raw + JPEG", burst: "Burst", manual: "Stack", split: "Split animation" };
  let current = null;        // the stack the window shows
  let viewerInfo = null;     // {stack, animated} of the file in the viewer

  /** @brief HTML-escape (the core helper when present). */

  function esc(s) { return (typeof _esc === "function") ? _esc(s) : String(s ?? ""); }
  /** @brief Show a short message in the core toast. */
  function toast(msg) { if (typeof showToast === "function") showToast(msg); }
  /** @brief Reload the gallery after a stack changed. */
  function reloadGrid() { if (typeof loadGallery === "function") loadGallery(); }

  /** @brief GET (no body) or POST JSON; the parsed reply, or {success:false} on a network error. */

  async function api(url, body) {
    const opts = body === undefined ? {} : {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
    };
    try {
      const r = await fetch(url, opts);
      return await r.json();
    } catch (e) {
      return { success: false, error: String(e) };
    }
  }

  /** @brief A small stacked-squares icon (the standard "layers / stack" glyph). */
  function stackIcon() {
    return '<svg viewBox="0 0 16 16" width="10" height="10" aria-hidden="true">' +
      '<rect x="5" y="1" width="10" height="10" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.6"/>' +
      '<rect x="1" y="5" width="10" height="10" rx="1.5" fill="currentColor"/></svg>';
  }

  // -- grid tiles -------------------------------------------------------------
  /** @brief Decorate the cover tile of a stack: layered card, count badge that opens it. */
  function decorateTile(tile, item) {
    if (!item || !item.stack) return;
    tile.classList.add("cim-stack");
    tile.dataset.stackId = item.stack.id;
    tile.dataset.stackKind = item.stack.kind;
    const b = document.createElement("span");
    b.className = "cim-stack-badge";
    b.title = (KIND_LABEL[item.stack.kind] || "Stack") + ": " + item.stack.count + " files - open the stack";
    b.innerHTML = stackIcon() + "<span>" + esc(item.stack.count) + "</span>";
    b.addEventListener("click", (e) => { e.stopPropagation(); openStack(item.stack.id); });
    tile.appendChild(b);
  }
  if (window.registerGalleryTileHook) registerGalleryTileHook(decorateTile);

  // -- stack window -------------------------------------------------------------
  /** @brief The stack window, built on first use. */
  function ensureModal() {
    let m = document.getElementById("stacks_modal");
    if (m) return m;
    m = document.createElement("div");
    m.id = "stacks_modal";
    m.className = "fixed inset-0 z-50 hidden items-center justify-center bg-black/70";
    m.setAttribute("data-feature", FEATURE);
    m.innerHTML =
      '<div class="cim-stacks-panel bg-gray-900 border border-gray-700 rounded-lg shadow-xl flex flex-col">' +
      '  <div class="flex items-center gap-2 px-3 py-2 border-b border-gray-700">' +
      '    <span id="stacks_title" class="text-sm font-bold text-gray-200 flex-1"></span>' +
      '    <button type="button" id="stacks_close" class="text-gray-400 hover:text-white text-lg leading-none" title="Close">&times;</button>' +
      '  </div>' +
      '  <div id="stacks_grid" class="cim-stacks-grid flex-1 overflow-auto p-3"></div>' +
      '  <div class="border-t border-gray-700 px-3 py-2 flex flex-wrap items-center gap-2" data-write-gate="' + FEATURE + '">' +
      cimButton({ label: "Set as cover", id: "stacks_btn_cover", variant: "secondary",
        title: "Make the selected (or open) file the tile the gallery shows" }) +
      cimButton({ label: "Remove selected", id: "stacks_btn_remove", variant: "warn",
        title: "Take the selected files out of this stack" }) +
      cimButton({ label: "Unstack", id: "stacks_btn_unstack", variant: "danger",
        title: "Dissolve this stack; every file shows on its own again" }) +
      '    <span class="flex-1"></span>' +
      '    <label class="text-[11px] text-gray-400">Animation</label>' +
      '    <select id="stacks_merge_fmt" class="bg-gray-800 border border-gray-600 rounded text-xs text-gray-200 px-1 py-0.5">' +
      '      <option value="jxl">JXL</option><option value="gif">GIF</option>' +
      '      <option value="webp">WebP</option><option value="apng">APNG</option></select>' +
      '    <input id="stacks_merge_delay" type="text" value="auto" size="5" title="Frame delay in ms, or auto (from the capture times)"' +
      '      class="bg-gray-800 border border-gray-600 rounded text-xs text-gray-200 px-1 py-0.5">' +
      '    <label class="text-[11px] text-gray-400 flex items-center gap-1" title="Delete the members once the animation is stored">' +
      '      <input id="stacks_merge_remove" type="checkbox"> remove members</label>' +
      cimButton({ label: "Merge to animation", id: "stacks_btn_merge", variant: "primary",
        title: "Store the stack's files as one animation (format per Settings -> Media)" }) +
      '  </div>' +
      '</div>';
    document.body.appendChild(m);
    m.addEventListener("click", (e) => { if (e.target === m) closeStack(); });
    m.querySelector("#stacks_close").addEventListener("click", closeStack);
    m.querySelector("#stacks_btn_cover").addEventListener("click", setCover);
    m.querySelector("#stacks_btn_remove").addEventListener("click", removeSelected);
    m.querySelector("#stacks_btn_unstack").addEventListener("click", unstack);
    m.querySelector("#stacks_btn_merge").addEventListener("click", merge);
    if (window.CIMFeatures && CIMFeatures.apply) CIMFeatures.apply(m);
    else if (window.applyFeatureVisibility) applyFeatureVisibility(m);
    return m;
  }

  /** @brief One member tile: shares the grid's selection; a plain click opens the file. */

  function memberTile(mem) {
    const div = document.createElement("div");
    div.className = "gallery-item cim-stack-member" + (mem.cover ? " cim-stack-cover" : "");
    div.dataset.filename = mem.filename;
    div.style.aspectRatio = (mem.width && mem.height) ? `${mem.width}/${mem.height}` : "1/1";
    const name = mem.filename.split("/").pop();
    let raw = "";
    if (mem.raw) {
      raw = mem.raw.uid
        ? `<a class="cim-stack-raw" href="/api/raw/open/${encodeURIComponent(mem.raw.uid)}" title="Download the raw ${esc(mem.raw.orig_name || "")}">RAW</a>`
        : `<span class="cim-stack-raw" title="Developed from ${esc(mem.raw.orig_name || "")} (raw not kept)">RAW</span>`;
    }
    div.innerHTML =
      `<img alt="" loading="lazy" class="loaded" src="/api/thumb/${encodeURIComponent(mem.filename)}">` +
      (mem.cover ? '<span class="cim-stack-cover-mark">cover</span>' : "") + raw +
      `<span class="label">${esc(name)}</span>` +
      '<span class="sel-check hidden absolute top-1 left-1 w-4 h-4 rounded-full bg-blue-500 border-2 border-white flex items-center justify-center text-[8px] font-bold text-white">&#10003;</span>';
    const a = div.querySelector("a.cim-stack-raw");
    if (a) a.addEventListener("click", (e) => e.stopPropagation());
    div.addEventListener("click", (e) => {
      if (e.ctrlKey || e.metaKey || e.shiftKey) {
        if (typeof handleGalleryClick === "function") handleGalleryClick(e, mem.filename);
        return;
      }
      closeStack();
      if (typeof handleGalleryClick === "function") handleGalleryClick(e, mem.filename);
      else if (typeof selectFile === "function") selectFile(mem.filename);
    });
    return div;
  }

  /** @brief Fill the stack window with a stack. */

  function render(stack) {
    current = stack;
    const m = ensureModal();
    m.querySelector("#stacks_title").textContent =
      `${KIND_LABEL[stack.kind] || "Stack"} - ${stack.count} files`;
    const grid = m.querySelector("#stacks_grid");
    grid.innerHTML = "";
    stack.members.forEach((mem) => grid.appendChild(memberTile(mem)));
    if (typeof refreshSelectionUI === "function") refreshSelectionUI();
  }

  /** @brief Open the stack window for a stack id. */
  async function openStack(id) {
    const d = await api(`/api/stacks/${encodeURIComponent(id)}`);
    if (!d.success) { toast(d.error || "Could not load the stack."); return; }
    render(d.stack);
    const m = ensureModal();
    m.classList.remove("hidden");
    m.classList.add("flex");
  }

  /** @brief Hide the stack window. */

  function closeStack() {
    const m = document.getElementById("stacks_modal");
    if (m) { m.classList.add("hidden"); m.classList.remove("flex"); }
    current = null;
  }

  /** @brief The selected files that belong to the open stack. */
  function selectedMembers() {
    if (!current) return [];
    const mine = new Set(current.members.map((x) => x.filename));
    const sel = (typeof selectedFiles !== "undefined") ? [...selectedFiles] : [];
    return sel.filter((f) => mine.has(f));
  }

  /** @brief Show the result of a stack edit and refresh the window and the grid. */

  async function afterChange(d, okMsg) {
    if (!d.success) { toast(d.error || "Failed."); return; }
    if (okMsg) toast(okMsg);
    if (d.stack) render(d.stack); else closeStack();
    reloadGrid();
  }

  /** @brief Make the selected member (or the open file) the cover. */

  async function setCover() {
    if (!current) return;
    const pick = selectedMembers()[0] ||
      (current.members.some((x) => x.filename === window.currentFile) ? window.currentFile : null);
    if (!pick) { toast("Select a file of this stack first (ctrl+click)."); return; }
    afterChange(await api(`/api/stacks/${current.id}/cover`, { filename: pick }), "Cover set.");
  }

  /** @brief Take the selected members out of the stack. */

  async function removeSelected() {
    if (!current) return;
    const files = selectedMembers();
    if (!files.length) { toast("Select the files to remove (ctrl+click)."); return; }
    files.forEach((f) => selectedFiles.delete(f));
    const d = await api(`/api/stacks/${current.id}/remove`, { filenames: files });
    afterChange(d, `${files.length} file(s) taken out of the stack.`);
  }

  /** @brief Dissolve the open stack. */

  async function unstack() {
    if (!current) return;
    afterChange(await api(`/api/stacks/${current.id}/unstack`, {}), "Unstacked.");
  }

  /** @brief Store the open stack as one animation and open it. */

  async function merge() {
    if (!current) return;
    const m = ensureModal();
    const btn = m.querySelector("#stacks_btn_merge");
    const delay = (m.querySelector("#stacks_merge_delay").value || "auto").trim();
    const body = {
      format: m.querySelector("#stacks_merge_fmt").value,
      delay_ms: /^\d+$/.test(delay) ? parseInt(delay, 10) : "auto",
      remove_members: m.querySelector("#stacks_merge_remove").checked,
    };
    btn.disabled = true;
    toast("Merging the stack into an animation...");
    const d = await api(`/api/stacks/${current.id}/merge`, body);
    btn.disabled = false;
    if (!d.success) { toast(d.error || "Merge failed."); return; }
    toast(`Stored ${d.filename} (${d.frames} frames).`);
    closeStack();
    reloadGrid();
    if (typeof selectFile === "function" && d.filename) selectFile(d.filename);
  }

  // -- bulk bar, review pane --------------------------------------------------------
  /** @brief Stack the gallery selection by hand. */
  async function stackSelected() {
    const files = (typeof selectedFiles !== "undefined") ? [...selectedFiles] : [];
    if (files.length < 2) { toast("Select at least two files to stack."); return; }
    const cover = files.includes(window.currentFile) ? window.currentFile : files[0];
    const d = await api("/api/stacks/create", { filenames: files, cover });
    if (!d.success) { toast(d.error || "Could not stack them."); return; }
    toast(`Stacked ${files.length} files.`);
    if (typeof clearSelection === "function") clearSelection();
    reloadGrid();
  }

  /** @brief Rescan the library: raw pairs, and bursts when burst stacking is on. */
  async function rescan() {
    const d = await api("/api/stacks/rescan", { raw: true, burst: true });
    toast(d.success ? "Stacks rescan started." : (d.error || "Rescan failed."));
  }

  // -- viewer: the open file's stack, split an animation -------------------------------
  /** @brief Show the viewer's Stack / Split buttons for the open file. */
  function syncViewerButtons() {
    const open = document.querySelectorAll(".stacks-open-btn");
    const split = document.querySelectorAll(".stacks-split-btn");
    const st = viewerInfo && viewerInfo.stack;
    open.forEach((b) => {
      b.style.display = st ? "" : "none";
      if (st) b.textContent = `Stack (${st.count})`;
    });
    split.forEach((b) => { b.style.display = (viewerInfo && viewerInfo.animated) ? "" : "none"; });
  }

  /** @brief Look up the stack and animation state of the file in the viewer. */

  async function refreshViewer(filename) {
    viewerInfo = null;
    syncViewerButtons();
    if (!filename) return;
    const d = await api(`/api/stacks/of?filename=${encodeURIComponent(filename)}`);
    if (window.currentFile !== filename || !d.success) return;
    viewerInfo = { stack: d.stack, animated: !!d.animated };
    syncViewerButtons();
  }

  /** @brief Open the stack of the file in the viewer. */

  async function openViewerStack() {
    if (viewerInfo && viewerInfo.stack) openStack(viewerInfo.stack.id);
  }

  /** @brief Split the animation in the viewer into a stack of stills. */

  async function splitCurrent() {
    const f = window.currentFile;
    if (!f) return;
    toast("Splitting the animation into a stack...");
    const d = await api("/api/stacks/split", { filename: f });
    if (!d.success) { toast(d.error || "Split failed."); return; }
    toast(`Split into ${d.files.length} frames.`);
    reloadGrid();
    if (d.stack) openStack(d.stack.id);
  }

  window.CIMStacks = { openStack, closeStack, stackSelected, rescan, openViewerStack, splitCurrent,
    refreshViewer, decorateTile };

  if (window.registerFileMetaHook) registerFileMetaHook((_meta, filename) => refreshViewer(filename || window.currentFile));

  /** @brief Register the bulk-bar, review-pane and viewer buttons. */

  function buildButtons() {
    if (!window.registerControlButton) return;
    registerControlButton("gallery_bulk", { label: "Stack selected", onclick: "CIMStacks.stackSelected()",
      feature: FEATURE, variant: "secondary", cls: "stacks-bulk-btn",
      title: "Show the selected files as one stack in the gallery" });
    registerControlButton("review_actions", { label: "Rescan stacks", onclick: "CIMStacks.rescan()",
      feature: FEATURE, variant: "secondary", cls: "stacks-rescan-btn",
      title: "Pair raws with their JPEGs across the library (and stack bursts when that is on)" });
    registerControlButton("viewer_toggles", { label: "Stack", onclick: "CIMStacks.openViewerStack()",
      feature: FEATURE, variant: "secondary", cls: "stacks-open-btn", hidden: true,
      title: "Open the stack this file belongs to" });
    registerControlButton("viewer_toggles", { label: "Split to stack", onclick: "CIMStacks.splitCurrent()",
      feature: FEATURE, variant: "tertiary", cls: "stacks-split-btn", hidden: true,
      title: "Store every frame of this animation as a still and stack them" });
  }
  if (document.readyState === "loading") window.addEventListener("DOMContentLoaded", buildButtons);
  else buildButtons();
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && current) closeStack();
  });
})();