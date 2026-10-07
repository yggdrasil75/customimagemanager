/* Search & Sort: a sort picker in the gallery toolbar. It rewrites the
 * `sort:` token in the search box, so the sort is part of the query (URL,
 * quick filters and typed tokens all keep working). */
(function () {
  const KEYS = [
    ["", "Sort: default"], ["path", "Path"], ["filename", "Filename"], ["width", "Width"], ["height", "Height"],
    ["pixels", "Pixels"], ["ratio", "Aspect ratio"], ["minside", "Short side"],
    ["maxside", "Long side"], ["date", "Date taken"], ["mtime", "Modified"],
    ["tags", "Tag count"], ["name", "Person name"], ["people", "People count"],
    ["rating", "Rating"],
  ];
  const SORT_RE = /(^|\s)sort:\S*/gi;

  function current() {
    const si = document.getElementById("search_input");
    const m = (si && si.value.match(/(?:^|\s)sort:(-?)([^\s:]+)/i)) || null;
    return m ? { key: m[2].toLowerCase(), desc: m[1] === "-" } : { key: "", desc: false };
  }

  function apply() {
    const si = document.getElementById("search_input");
    const key = document.getElementById("ss_sort_key").value;
    const desc = document.getElementById("ss_sort_dir").dataset.desc === "1";
    let q = si.value.replace(SORT_RE, " ").replace(/\s+/g, " ").trim();
    if (key || desc) q = (q + " sort:" + (desc ? "-" : "") + (key || "path")).trim();
    si.value = q;
    si.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function sync() {
    const sel = document.getElementById("ss_sort_key");
    const dir = document.getElementById("ss_sort_dir");
    if (!sel || !dir) return;
    const c = current();
    sel.value = KEYS.some(k => k[0] === c.key) ? c.key : "";
    dir.dataset.desc = c.desc ? "1" : "0";
    dir.textContent = c.desc ? "Desc" : "Asc";
  }

  const opts = KEYS.map(([v, l]) => `<option value="${v}">${l}</option>`).join("");
  registerControlButton("gallery_tools",
    `<span class="flex items-center gap-1" id="ss_sort_wrap">
       <select id="ss_sort_key" title="Sort the gallery (adds a sort: token to the search)"
         class="bg-gray-700 text-xs rounded px-1 py-1">${opts}</select>
       <button type="button" id="ss_sort_dir" data-desc="0" title="Ascending / descending"
         class="bg-gray-700 hover:bg-gray-600 text-xs rounded px-2 py-1">Asc</button>
     </span>`);

  document.addEventListener("change", e => { if (e.target.id === "ss_sort_key") apply(); });
  document.addEventListener("click", e => {
    if (e.target.id !== "ss_sort_dir") return;
    e.target.dataset.desc = e.target.dataset.desc === "1" ? "0" : "1";
    apply();
  });
  document.addEventListener("input", e => { if (e.target.id === "search_input") sync(); });
  sync();
})();