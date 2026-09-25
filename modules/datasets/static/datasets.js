/* Settings › Datasets — one-click zoo downloads + a link box for open hosts.
 *
 * Zoos (pyiqa IQA mirror, installed ultralytics YAMLs) come from
 * /api/datasets/zoo; every download is a "dataset:…" target on the shared
 * fetch queue (/api/fetch/add). Queue rows reuse fetch.js's row renderer.
 * Pane element: #settings_pane_module_datasets (created by static/modules.js);
 * the credential fields render into its #module_settings_fields_datasets mount. */
(function () {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => (window._esc ? _esc(s) : String(s));
  let _timer = null;
  let _wasBusy = false;

  async function queue(targets) {
    const r = await fetch("/api/fetch/add", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ targets }) }).then((x) => x.json()).catch(() => null);
    status(r && r.success ? `Queued ${r.added} dataset${r.added === 1 ? "" : "s"}.` : "Could not queue.",
           r && r.success ? "ok" : "err");
    refreshQueue();
  }

  function status(msg, kind) {
    const el = $("ds_status");
    if (!el) return;
    el.textContent = msg || "";
    el.className = "text-xs " + (kind === "err" ? "text-rose-400" : kind === "ok" ? "text-emerald-400" : "text-gray-400");
  }

  function shell() {
    const pane = $("settings_pane_module_datasets");
    if (!pane || $("ds_root")) return;
    pane.insertAdjacentHTML("beforeend", `
<div id="ds_root" class="space-y-4 text-xs">
  <p class="text-gray-400">Datasets download into <code id="ds_dir" class="text-gray-300"></code> (not the library)
    and are added to the Dedup and IQA trainers' dataset lists when done.</p>
  <div>
    <div class="font-bold text-gray-300 mb-1">From a link</div>
    <textarea id="ds_links" rows="3" class="w-full p-2 bg-gray-700 rounded border border-gray-600 text-xs text-white font-mono"
      placeholder="https://huggingface.co/datasets/owner/name split=train score=MOS&#10;https://www.kaggle.com/datasets/owner/name&#10;https://zenodo.org/records/1234567&#10;https://example.org/images.zip name=myset"></textarea>
    <p class="text-[10px] text-gray-500 mt-1">One per line: Hugging Face, Kaggle, Zenodo or any archive / parquet / CSV URL.
      Options after the link: name=&lt;folder&gt; score=&lt;column&gt; image=&lt;parquet column&gt; split=&lt;text&gt; rev=&lt;branch&gt;.
      Lines with the same name= share a folder (e.g. images zip + scores zip).</p>
    <button id="ds_queue_links" class="mt-1 bg-indigo-600 hover:bg-indigo-500 px-3 py-1 rounded font-bold">Queue</button>
    <span id="ds_status" class="ml-2"></span>
  </div>
  <div id="ds_zoos" class="space-y-3"></div>
  <div>
    <div class="font-bold text-gray-300 mb-1">Queue</div>
    <div id="ds_queue" class="max-h-48 overflow-y-auto space-y-1"></div>
  </div>
</div>`);
    $("ds_queue_links").addEventListener("click", () => {
      const lines = $("ds_links").value.split("\n").map((s) => s.trim()).filter(Boolean)
        .map((s) => (s.toLowerCase().startsWith("dataset:") ? s : "dataset:" + s));
      if (!lines.length) { status("Enter at least one link.", "err"); return; }
      $("ds_links").value = "";
      queue(lines);
    });
    $("ds_zoos").addEventListener("click", (ev) => {
      const b = ev.target.closest("[data-target]");
      if (b) queue([b.dataset.target]);
    });
  }

  async function loadZoos() {
    const r = await fetch("/api/datasets/zoo").then((x) => x.json()).catch(() => null);
    if (!r || !r.success) return;
    $("ds_dir").textContent = r.root;
    const have = new Set(r.have || []);
    $("ds_zoos").innerHTML = r.zoos.map((z) => `
      <details ${z.id === "pyiqa" ? "open" : ""} class="bg-gray-900/40 rounded p-2">
        <summary class="cursor-pointer font-bold text-gray-300">${esc(z.label)}
          <span class="text-gray-500 font-normal">(${z.items.length}${z.labelled ? ", quality-labelled: IQA + Dedup" : ", unlabelled: Dedup"})</span></summary>
        ${z.items.length ? "" : '<div class="text-gray-500 mt-1">Not available (package not installed).</div>'}
        <div class="mt-1 space-y-0.5 max-h-64 overflow-y-auto">
          ${z.items.map((it) => `
            <div class="flex items-center gap-2 px-1 py-0.5 hover:bg-gray-800 rounded">
              <span class="flex-1 truncate text-gray-300" title="${esc(it.target)}">${esc(it.label)}</span>
              ${have.has(it.name) ? '<span class="text-emerald-400 text-[10px]">downloaded</span>' : ""}
              <button data-target="${esc(it.target)}" class="text-sky-400 hover:text-sky-300 font-bold">
                ${have.has(it.name) ? "Re-fetch" : "Download"}</button>
            </div>`).join("")}
        </div>
      </details>`).join("");
  }

  async function refreshQueue() {
    const wrap = $("ds_queue");
    if (!wrap) return;
    const r = await fetch("/api/fetch/queue").then((x) => x.json()).catch(() => null);
    const rows = ((r && r.queue) || []).filter((q) => q.fetcher === "datasets");
    wrap.innerHTML = rows.length && window._gdlQueueRow ? rows.map(_gdlQueueRow).join("")
      : '<div class="text-gray-600 text-[10px]">Nothing queued.</div>';
    const pane = $("settings_pane_module_datasets");
    const busy = rows.some((q) => q.status === "pending" || q.status === "downloading");
    if (_wasBusy && !busy) loadZoos();          // a download finished: update the "downloaded" marks
    _wasBusy = busy;
    clearTimeout(_timer);
    if (pane && !pane.classList.contains("hidden")) _timer = setTimeout(refreshQueue, busy ? 3000 : 15000);
  }

  document.addEventListener("module-settings-tab", (ev) => {
    if (ev.detail !== "datasets") return;
    shell();
    loadZoos();
    refreshQueue();
  });
})();