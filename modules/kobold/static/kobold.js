/* Kobold module: Settings → Kobold tab. Start/stop the local koboldcpp
   server, show its log, and the module's settings fields inline. */
(function () {
  let timer = null;

  function pane() { return document.getElementById("settings_pane_module_kobold"); }

  function render() {
    const p = pane();
    if (!p || p.dataset.built) return;
    p.dataset.built = "1";
    p.innerHTML = `
      <div class="text-xs text-gray-300 space-y-2">
        <div class="flex items-center gap-2">
          <button id="kobold_start" class="bg-emerald-700 hover:bg-emerald-600 px-3 py-1 rounded font-bold">▶ Start</button>
          <button id="kobold_stop" class="bg-red-800 hover:bg-red-700 px-3 py-1 rounded font-bold">■ Stop</button>
          <span id="kobold_state" class="text-gray-400"></span>
        </div>
        <div class="flex items-center gap-2 flex-wrap">
          <span class="text-gray-400">No koboldcpp?</span>
          <select id="kobold_build" class="bg-gray-900 border border-gray-700 rounded px-2 py-1"></select>
          <button id="kobold_download" class="bg-gray-700 hover:bg-gray-600 px-3 py-1 rounded font-bold">⬇ Download build</button>
          <a id="kobold_releases" target="_blank" class="text-cyan-400 hover:text-cyan-300">all releases ↗</a>
        </div>
        <div id="kobold_fields" class="grid gap-2 md:grid-cols-2"></div>
        <pre id="kobold_log" class="bg-black/60 border border-gray-700 rounded p-2 h-56 overflow-auto text-[10px] font-mono whitespace-pre-wrap"></pre>
      </div>`;
    const fields = (window._moduleFields || {}).kobold || [];
    const box = document.getElementById("kobold_fields");
    if (window.moduleFieldEl) for (const f of fields) box.appendChild(moduleFieldEl(f));
    document.getElementById("kobold_start").addEventListener("click", () => post("/api/kobold/start"));
    document.getElementById("kobold_stop").addEventListener("click", () => post("/api/kobold/stop"));
    document.getElementById("kobold_download").addEventListener("click", () =>
      post("/api/kobold/download", { variant: document.getElementById("kobold_build").value }));
    poll();
  }

  async function post(url, body) {
    try {
      const d = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {}) }).then(r => r.json());
      if (!d.success && window.showToast) showToast(d.error || "kobold: failed");
      show(d);
    } catch (e) { if (window.showToast) showToast("kobold: " + e); }
  }

  function show(d) {
    const st = document.getElementById("kobold_state");
    const log = document.getElementById("kobold_log");
    if (!st || !log) return;
    const sel = document.getElementById("kobold_build");
    if (sel && !sel.options.length) for (const b of d.builds || []) {
      const o = document.createElement("option"); o.value = b.value; o.textContent = b.label; sel.appendChild(o);
    }
    const rel = document.getElementById("kobold_releases");
    if (rel && d.releases_url) rel.href = d.releases_url;
    const dl = document.getElementById("kobold_download");
    if (dl) dl.disabled = !!d.downloading;
    st.textContent = d.downloading ? "downloading koboldcpp…" : !d.running ? "stopped"
      : d.ready ? `ready at ${d.url}` + (d.endpoint_applied ? " · VLM endpoint set" : "")
      : `starting (pid ${d.pid})…`;
    const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 4;
    log.textContent = (d.log || []).join("\n");
    if (atBottom) log.scrollTop = log.scrollHeight;
  }

  async function poll() {
    clearTimeout(timer);
    const p = pane();
    if (!p || p.classList.contains("hidden")) return;
    try { show(await fetch("/api/kobold/status").then(r => r.json())); } catch (e) { }
    timer = setTimeout(poll, 2000);
  }

  document.addEventListener("module-settings-tab", (ev) => {
    if (ev.detail === "kobold") { render(); poll(); }
  });
})();