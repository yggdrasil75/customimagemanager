/* Settings › Fetch sites — per-site gallery-dl config in one place.
 *
 * Left: every site we know (anything with fields, a mapping, opts or a login).
 * Right: the selected site's login on top, then every field ever discovered
 * for it with its mapping target. Fields only accumulate (a video post shows
 * keys an image post doesn't); the user can hide the ones they don't want.
 *
 * Reuses the row builder / target options / mapping reader from fetch.js.
 * Pane element: #settings_pane_module_gdl_sites (created by static/modules.js). */
(function () {
  let _site = "";
  let _rec = null;                 // {site, fields, hidden, mapping, opts, auth}
  let _showHidden = false;
  const $ = (id) => document.getElementById(id);
  const inp = "w-full p-2 bg-gray-700 rounded border border-gray-600 text-xs text-white";

  async function post(url, body) {
    return fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}) }).then((r) => r.json()).catch(() => null);
  }

  function status(msg, kind) {
    const el = $("gs_status");
    if (!el) return;
    el.textContent = msg || "";
    el.className = "text-xs " + (kind === "err" ? "text-rose-400"
      : kind === "ok" ? "text-emerald-400" : "text-gray-400");
  }

  // ── shell ──────────────────────────────────────────────────────────────
  function shell() {
    const pane = $("settings_pane_module_gdl_sites");
    if (!pane || $("gs_root")) return pane;
    pane.insertAdjacentHTML("beforeend", `
<div id="gs_root" class="flex gap-3 h-full min-h-[420px]">
  <div class="w-44 flex-shrink-0 flex flex-col border-r border-gray-700 pr-2">
    <div class="flex items-center justify-between mb-2">
      <span class="text-xs font-bold text-gray-400">Sites</span>
      <button id="gs_add" class="text-xs bg-indigo-600 hover:bg-indigo-500 px-2 py-0.5 rounded font-bold">+ Add</button>
    </div>
    <div id="gs_list" class="flex-1 overflow-y-auto space-y-0.5 text-xs"></div>
  </div>
  <div id="gs_detail" class="flex-1 min-w-0 overflow-y-auto pr-1">
    <p class="text-xs text-gray-500">Pick a site on the left, or add one by URL.</p>
  </div>
</div>`);
    $("gs_add").addEventListener("click", addSite);
    return pane;
  }

  // ── site list ──────────────────────────────────────────────────────────
  async function loadList() {
    const r = await fetch("/api/gdl/sites").then((x) => x.json()).catch(() => null);
    const list = $("gs_list");
    if (!list) return;
    const sites = (r && r.sites) || [];
    if (!sites.length) {
      list.innerHTML = '<div class="text-gray-600 text-[10px]">No sites yet.</div>';
      return;
    }
    list.innerHTML = sites.map((s) => `
<button data-site="${_esc(s.site)}" class="gs-site w-full text-left px-2 py-1 rounded hover:bg-gray-700
  ${s.site === _site ? "bg-gray-700 text-white" : "text-gray-300"}">
  <div class="truncate font-mono">${_esc(s.site)}</div>
  <div class="text-[10px] text-gray-500">${s.mapped}/${s.fields} mapped${s.auth !== "none" ? " · login" : ""}</div>
</button>`).join("");
    list.querySelectorAll(".gs-site").forEach((b) =>
      b.addEventListener("click", () => select(b.dataset.site)));
  }

  async function addSite() {
    const v = (window.prompt("Site URL (any gallery-dl-supported page), or a gallery-dl site name:") || "").trim();
    if (!v) return;
    let site = v;
    if (/^https?:\/\//i.test(v)) {
      const r = await post("/api/gdl/site", { url: v });
      if (!r || !r.success) { status(r?.error || "Unknown site.", "err"); return; }
      site = r.site;
    } else {
      await post("/api/gdl/config", { site, mapping: {} });
    }
    await loadList();
    await select(site);
    if (/^https?:\/\//i.test(v)) {
      $("gs_url").value = v;
      discover();
    }
  }

  // ── detail ─────────────────────────────────────────────────────────────
  async function select(site) {
    _site = site;
    const r = await post("/api/gdl/sites", { site });
    if (!r || !r.success) { status("Could not load site.", "err"); return; }
    _rec = r;
    await loadList();
    await renderDetail();
  }

  async function renderDetail() {
    const d = $("gs_detail");
    const a = _rec.auth || { method: "none" };
    d.innerHTML = `
<div class="flex items-center justify-between mb-2">
  <span class="text-sm font-bold text-emerald-400 font-mono">${_esc(_site)}</span>
  <div class="flex items-center gap-3">
    <span id="gs_status" class="text-xs text-gray-400"></span>
    <button id="gs_forget" class="text-[10px] text-rose-400 hover:text-rose-300">forget site</button>
  </div>
</div>

<div class="border border-gray-700 rounded p-2 mb-3">
  <label class="text-xs text-gray-400 font-bold block mb-1">Login</label>
  <select id="gs_auth_method" class="${inp} mb-2">
    <option value="none">None (public)</option>
    <option value="userpass">Username &amp; password</option>
    <option value="cookies_text">Cookies (paste)</option>
    <option value="cookies_browser">Cookies from a browser</option>
  </select>
  <div id="gs_auth_userpass" class="hidden space-y-2 mb-2">
    <input id="gs_auth_user" type="text" placeholder="username" class="${inp}" value="${_esc(a.username || "")}">
    <input id="gs_auth_pass" type="password" class="${inp}"
      placeholder="${a.has_password ? "password (on file — leave blank to keep)" : "password"}">
  </div>
  <div id="gs_auth_cookies" class="hidden mb-2">
    <textarea id="gs_auth_cookies_text" rows="3" class="${inp} font-mono text-[11px]"
      placeholder="${a.has_cookies ? "Cookies on file — paste again to replace" : "Paste cookies.txt (Netscape) or name=value; name2=value2"}"></textarea>
  </div>
  <div id="gs_auth_browser" class="hidden mb-2">
    <select id="gs_auth_browser_sel" class="${inp}">
      ${["firefox","chrome","chromium","edge","brave","vivaldi","opera","safari"].map((b) =>
        `<option value="${b}" ${a.browser === b ? "selected" : ""}>${b}</option>`).join("")}
    </select>
  </div>
</div>

<div class="border border-gray-700 rounded p-2 mb-3">
  <div class="flex items-center justify-between mb-1">
    <label class="text-xs text-gray-400 font-bold">Fields
      <span class="text-gray-600 font-normal">(${_rec.fields.length} known, ${_rec.hidden.length} hidden)</span></label>
    <label class="text-[10px] text-gray-500 flex items-center gap-1">
      <input id="gs_show_hidden" type="checkbox" ${_showHidden ? "checked" : ""}> show hidden</label>
  </div>
  <div class="flex gap-2 mb-2">
    <input id="gs_url" type="text" placeholder="Sample post/gallery URL to (re)discover fields" class="${inp} flex-1">
    <button id="gs_discover" class="text-xs bg-teal-700 hover:bg-teal-600 px-3 py-1 rounded font-bold whitespace-nowrap">Check fields</button>
  </div>
  <p class="text-[10px] text-gray-600 mb-1">New fields are added to this list; nothing is ever removed.
    Use 👁 to hide a field you don't care about.</p>
  <div class="flex items-center gap-2 pb-1 mb-1 border-b border-gray-700 text-[10px] uppercase tracking-wide text-gray-500">
    <span class="w-6"></span><span class="flex-1">gallery-dl field</span><span class="w-52">maps to</span>
  </div>
  <div id="gs_rows" class="max-h-72 overflow-y-auto pr-1"></div>
</div>

<details class="mb-3">
  <summary class="text-[10px] text-gray-500 cursor-pointer">Advanced: raw gallery-dl options</summary>
  <textarea id="gs_opts" rows="2" class="${inp} mt-1 font-mono"
    placeholder="danbooru.api-key=...&#10;extractor.timeout=30">${_esc((_rec.opts || []).join("\n"))}</textarea>
</details>

<div class="flex justify-end">
  <button id="gs_save" class="text-sm bg-emerald-700 hover:bg-emerald-600 px-4 py-1.5 rounded font-bold">Save site</button>
</div>`;

    $("gs_auth_method").value = a.method || "none";
    const authToggle = () => {
      const m = $("gs_auth_method").value;
      $("gs_auth_userpass").classList.toggle("hidden", m !== "userpass");
      $("gs_auth_cookies").classList.toggle("hidden", m !== "cookies_text");
      $("gs_auth_browser").classList.toggle("hidden", m !== "cookies_browser");
    };
    $("gs_auth_method").addEventListener("change", authToggle);
    authToggle();
    $("gs_show_hidden").addEventListener("change", (e) => { _showHidden = e.target.checked; applyHidden(); });
    $("gs_discover").addEventListener("click", discover);
    $("gs_save").addEventListener("click", save);
    $("gs_forget").addEventListener("click", forget);
    await renderRows();
  }

  async function renderRows() {
    const wrap = $("gs_rows");
    wrap.innerHTML = "";
    const optsHTML = await _gdlTargetOptionsHTML();
    const saved = _rec.mapping || {};
    const hidden = new Set(_rec.hidden || []);
    for (const f of _rec.fields) {
      const row = _gdlFieldRow(f, _gdlTargetFor(f, saved), optsHTML);
      row.dataset.hidden = hidden.has(f) ? "1" : "";
      const eye = document.createElement("button");
      eye.className = "gs-eye w-6 text-xs text-gray-500 hover:text-white";
      eye.title = "hide / show this field";
      eye.addEventListener("click", () => {
        row.dataset.hidden = row.dataset.hidden ? "" : "1";
        applyHidden();
      });
      row.prepend(eye);
      wrap.appendChild(row);
    }
    applyHidden();
  }

  function applyHidden() {
    $("gs_rows").querySelectorAll(".gdl-map-row").forEach((row) => {
      const h = !!row.dataset.hidden;
      row.querySelector(".gs-eye").textContent = h ? "🚫" : "👁";
      row.classList.toggle("opacity-40", h);
      row.classList.toggle("hidden", h && !_showHidden);
    });
  }

  function hiddenList() {
    return [...$("gs_rows").querySelectorAll(".gdl-map-row")]
      .filter((r) => r.dataset.hidden).map((r) => r.dataset.field);
  }

  async function save() {
    const m = $("gs_auth_method").value;
    const auth = { method: m };
    if (m === "userpass") {
      auth.username = $("gs_auth_user").value.trim();
      const p = $("gs_auth_pass").value;
      if (p) auth.password = p;
    } else if (m === "cookies_text") {
      const t = $("gs_auth_cookies_text").value;
      if (t.trim()) auth.cookies_text = t;      // blank = keep what's on file
    } else if (m === "cookies_browser") {
      auth.browser = $("gs_auth_browser_sel").value;
    }
    const r = await post("/api/gdl/config", {
      site: _site, auth, mapping: _gdlCurrentMapping($("gs_rows")),
      hidden: hiddenList(), opts: $("gs_opts").value });
    status(r?.success ? "Saved." : "Save failed.", r?.success ? "ok" : "err");
    if (r?.success) select(_site);
  }

  async function discover() {
    const url = $("gs_url").value.trim();
    if (!url) { status("Enter a sample URL first.", "err"); return; }
    status("Checking fields…");
    // persist current edits first so discovery doesn't overwrite them on reload
    await post("/api/gdl/config", { site: _site, mapping: _gdlCurrentMapping($("gs_rows")),
      hidden: hiddenList() });
    const r = await post("/api/gdl/fields", { url });
    if (!r || !r.success) { status(r?.error || "Could not read fields.", "err"); return; }
    if (r.site && r.site !== _site) { status(`That URL is ${r.site}, not ${_site}.`, "err"); await loadList(); return; }
    const before = _rec.fields.length;
    await select(_site);
    status(`${_rec.fields.length - before} new field(s); ${_rec.fields.length} known.`, "ok");
    $("gs_url").value = url;
  }

  async function forget() {
    if (!window.confirm(`Forget everything about ${_site} (fields, mapping, login, options)?`)) return;
    await post("/api/gdl/config", { site: _site, forget: true });
    _site = ""; _rec = null;
    $("gs_detail").innerHTML = '<p class="text-xs text-gray-500">Pick a site on the left, or add one by URL.</p>';
    loadList();
  }

  document.addEventListener("module-settings-tab", (ev) => {
    if (ev.detail !== "gdl_sites") return;
    shell();
    loadList();
    if (_site) select(_site);
  });
})();