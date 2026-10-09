/* Image-to-image search. A picture icon inside the gallery search box opens a
 * small popover: drop or pick an image file, paste one from the clipboard, or
 * use the picture open in the viewer. An image dropped straight onto the
 * search box searches too. The picture goes to /api/embedding/search_image and
 * the ranked library fills the grid, best first, the way the Similar button
 * shows its results. */
(function () {
  const URL_SEARCH = "/api/embedding/search_image";
  const TOP_K = 60;
  const IMG_EXT = /\.(jpe?g|png|webp|bmp|gif|apng|jxl|heic|heif|hif|avif|tiff?)$/i;
  let busy = false;
  const $ = id => document.getElementById(id);
  const toast = m => (typeof showToast === "function" ? showToast(m) : null);

  /** @brief May this user run image search (the icon is not feature-hidden)? */
  function allowed() {
    const w = $("is_wrap");
    return !!w && !w.classList.contains("cim-feature-hidden");
  }

  /** @brief Is this File an image we can send? */
  function isImage(f) {
    return !!f && (/^image\//.test(f.type || "") || IMG_EXT.test(f.name || ""));
  }

  /** @brief The first image File in a DataTransfer / clipboard, or null. */
  function imageFrom(dt) {
    if (!dt) return null;
    for (const f of dt.files || []) if (isImage(f)) return f;
    for (const it of dt.items || []) {
      if (it.kind === "file" && /^image\//.test(it.type || "")) {
        const f = it.getAsFile();
        if (f) return f;
      }
    }
    return null;
  }

  /** @brief Show a fixed result set in the grid (same as the Similar button). */
  function show(files) {
    if (typeof totalFiles !== "undefined") totalFiles = files.length;
    renderGallery(files);
    if (typeof updatePager === "function") updatePager();
    toast(files.length ? `${files.length} matching picture(s) - best first.` : "No matching pictures.");
  }

  /** @brief POST a search (FormData with `file`, or {filename}) and show the result. */
  async function run(body) {
    if (busy) return;
    busy = true;
    setStatus("Searching...");
    const form = body instanceof FormData;
    let d;
    try {
      const r = await fetch(URL_SEARCH, form ? { method: "POST", body }
        : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      d = await r.json();
    } catch (e) {
      d = { success: false, error: "Network error." };
    } finally {
      busy = false;
    }
    if (!d || !d.success) {
      setStatus((d && d.error) || "Image search failed.");
      toast((d && d.error) || "Image search failed.");
      return;
    }
    setStatus("");
    toggle(false);
    show(d.files || []);
  }

  /** @brief Search the library with an image File. */
  function searchFile(file) {
    if (!isImage(file)) { toast("That is not an image."); return; }
    const fd = new FormData();
    fd.append("file", file, file.name || "pasted.png");
    fd.append("top_k", String(TOP_K));
    run(fd);
  }

  /** @brief Search the library with the picture open in the viewer. */
  function searchCurrent() {
    const fn = window.currentFile;
    if (!fn) { toast("Open a picture first."); return; }
    run({ filename: fn, top_k: TOP_K });
  }

  /** @brief The popover's one-line status. */
  function setStatus(msg) {
    const s = $("is_status");
    if (s) { s.textContent = msg || ""; s.classList.toggle("hidden", !msg); }
  }

  /** @brief Show or hide the popover. */
  function toggle(force) {
    const pop = $("is_pop");
    if (!pop) return;
    const open = force !== undefined ? !!force : pop.classList.contains("hidden");
    pop.classList.toggle("hidden", !open);
    if (!open) return;
    if (typeof hideQuickFilters === "function") hideQuickFilters();
    setStatus("");
    const cur = $("is_current");
    if (cur) cur.disabled = !window.currentFile;
  }

  const icon = `<svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
      <rect x="1.5" y="2.5" width="13" height="11" rx="1.5"/><circle cx="5.5" cy="6" r="1.3"/><path d="M2 12l4-4 3 3 2-2 3 3"/></svg>`;

  registerControlButton("search_tools",
    `<span class="relative flex items-center" id="is_wrap" data-feature="tab.review">
       <button type="button" id="is_btn" title="Search by image" aria-label="Search by image"
         class="cim-btn cim-btn-neutral cim-btn-xs text-gray-300">${icon}</button>
       <div id="is_pop" class="hidden absolute right-0 top-full mt-2 z-30 bg-gray-800 border border-gray-600 rounded shadow-lg p-2 w-64 flex flex-col gap-2 text-xs text-gray-200">
         <div id="is_drop" tabindex="0"
           class="is-drop border border-dashed border-gray-500 rounded p-3 text-center text-gray-400 cursor-pointer">
           Drop an image here, click to pick one, or paste (Ctrl+V)
         </div>
         <input id="is_file" type="file" accept="image/*,.jxl,.heic,.heif" class="hidden">
         ${cimButton({ label: "Use current picture", id: "is_current", variant: "secondary", size: "xs" })}
         <div id="is_status" class="hidden text-gray-400"></div>
       </div>
     </span>`);

  document.addEventListener("click", e => {
    const t = e.target;
    if (!(t instanceof Element)) return;
    if (t.closest("#is_btn")) { toggle(); return; }
    if (t.closest("#is_drop")) { const f = $("is_file"); if (f) f.click(); return; }
    if (t.closest("#is_current")) { searchCurrent(); return; }
    const pop = $("is_pop");
    if (pop && !pop.classList.contains("hidden") && !t.closest("#is_pop")) toggle(false);
  });
  document.addEventListener("change", e => {
    if (e.target && e.target.id === "is_file") {
      const f = e.target.files && e.target.files[0];
      e.target.value = "";
      if (f) searchFile(f);
    }
  });
  // Paste: while the popover is open, or into the search box itself.
  document.addEventListener("paste", e => {
    const pop = $("is_pop");
    const open = pop && !pop.classList.contains("hidden");
    if (!allowed() || (!open && !(e.target && e.target.id === "search_input"))) return;
    const f = imageFrom(e.clipboardData);
    if (!f) return;
    e.preventDefault();
    searchFile(f);
  });
  // Drag and drop onto the popover's drop zone or straight onto the search box.
  const dropTarget = t => allowed() && t instanceof Element && (t.closest("#is_drop") || t.id === "search_input") ? t : null;
  const carriesFiles = e => !!(e.dataTransfer && [...(e.dataTransfer.types || [])].includes("Files"));
  document.addEventListener("dragover", e => {
    const t = dropTarget(e.target);
    if (!t || !carriesFiles(e)) return;
    e.preventDefault();
    e.stopPropagation();
    if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
    t.classList.add("is-drag-over");
  });
  document.addEventListener("dragleave", e => {
    const t = dropTarget(e.target);
    if (t) t.classList.remove("is-drag-over");
  });
  document.addEventListener("drop", e => {
    const t = dropTarget(e.target);
    if (!t) return;
    t.classList.remove("is-drag-over");
    const f = imageFrom(e.dataTransfer);
    if (!f) return;
    e.preventDefault();
    e.stopPropagation();
    searchFile(f);
  });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && e.target instanceof Element && e.target.closest("#is_pop")) toggle(false);
    if (e.key === "Enter" && e.target instanceof Element && e.target.id === "is_drop") $("is_file")?.click();
  });
})();
