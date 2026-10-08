/* Search & Sort: a sort picker inside the gallery search box. One small icon
 * button at the right edge of the box opens a popover with the sort keys and
 * the direction. It rewrites the `sort:` token in the search box, so the sort
 * is part of the query (URL, quick filters and typed tokens all keep working);
 * the icon lights up while a sort is set. */
(function () {
  const KEYS = [
    ["", "Default"], ["path", "Path"], ["filename", "Filename"], ["date", "Date taken"], ["mtime", "Modified"],
    ["width", "Width"], ["height", "Height"], ["pixels", "Pixels"], ["ratio", "Aspect ratio"],
    ["minside", "Short side"], ["maxside", "Long side"], ["tags", "Tag count"],
    ["name", "Person name"], ["people", "People count"], ["rating", "Rating"],
  ];
  const SORT_RE = /(^|\s)sort:\S*/gi;

  /** @brief The sort the search box currently carries: {key, desc}. */
  function current() {
    const si = document.getElementById("search_input");
    const m = (si && si.value.match(/(?:^|\s)sort:(-?)([^\s:]+)/i)) || null;
    return m ? { key: m[2].toLowerCase(), desc: m[1] === "-" } : { key: "", desc: false };
  }

  /** @brief Rewrite the sort: token for `key` / `desc` and re-run the search. */
  function apply(key, desc) {
    const si = document.getElementById("search_input");
    let q = si.value.replace(SORT_RE, " ").replace(/\s+/g, " ").trim();
    if (key || desc) q = (q + " sort:" + (desc ? "-" : "") + (key || "path")).trim();
    si.value = q;
    si.dispatchEvent(new Event("input", { bubbles: true }));
  }

  /** @brief Redraw the popover rows and the icon's active state from the box. */
  function sync() {
    const c = current();
    const btn = document.getElementById("ss_sort_btn");
    if (btn) {
      btn.classList.toggle("ss-sort-on", !!(c.key || c.desc));
      const label = (KEYS.find(k => k[0] === c.key) || KEYS[0])[1];
      btn.title = c.key ? `Sorted by ${label}${c.desc ? " (descending)" : ""}` : "Sort";
    }
    const pop = document.getElementById("ss_sort_pop");
    if (!pop) return;
    pop.querySelectorAll("[data-ss-key]").forEach(r => {
      const on = (r.dataset.ssKey || "") === c.key;
      r.classList.toggle("ss-sort-row-on", on);
    });
    const dir = pop.querySelector("#ss_sort_dir");
    if (dir) dir.textContent = c.desc ? "Descending" : "Ascending";
  }

  /** @brief Show or hide the popover. */
  function toggle(force) {
    const pop = document.getElementById("ss_sort_pop");
    if (!pop) return;
    const open = force !== undefined ? !!force : pop.classList.contains("hidden");
    pop.classList.toggle("hidden", !open);
    if (open) { sync(); if (typeof hideQuickFilters === "function") hideQuickFilters(); }
  }

  const rows = KEYS.map(([v, l]) =>
    `<button type="button" data-ss-key="${v}" class="ss-sort-row">${l}</button>`).join("");
  registerControlButton("search_tools",
    `<span class="relative flex items-center" id="ss_sort_wrap">
       <button type="button" id="ss_sort_btn" title="Sort"
         class="cim-btn cim-btn-neutral cim-btn-xs text-gray-300">&#8645;</button>
       <div id="ss_sort_pop"
         class="hidden absolute right-0 top-full mt-2 z-30 bg-gray-800 border border-gray-600 rounded shadow-lg p-1 w-44 flex flex-col text-xs">
         <button type="button" id="ss_sort_dir" class="ss-sort-row ss-sort-dir">Ascending</button>
         <div class="border-t border-gray-700 my-1"></div>
         ${rows}
       </div>
     </span>`);

  document.addEventListener("click", e => {
    const t = e.target;
    if (!(t instanceof Element)) return;
    if (t.closest("#ss_sort_btn")) { toggle(); return; }
    const row = t.closest("[data-ss-key]");
    if (row) { apply(row.dataset.ssKey, current().desc); toggle(false); return; }
    if (t.closest("#ss_sort_dir")) { const c = current(); apply(c.key, !c.desc); return; }
    if (!t.closest("#ss_sort_pop")) toggle(false);
  });
  document.addEventListener("input", e => { if (e.target.id === "search_input") sync(); });
  sync();
})();
