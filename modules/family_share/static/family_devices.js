/* Family share - Settings -> My devices.
 *
 * Any signed-in user pairs their own phones (the CIM Family app) here. A phone
 * acts as the account that paired it: it sees that account's personal folder
 * (scope "personal") or everything the account can see (scope "all"), and its
 * uploads land in the account's personal folder and count against its quota.
 * The core owns the tab button and the pane (settings_pane_module_family_devices);
 * this fills it on the 'module-settings-tab' event. All state is on the server
 * (/api/family_share/devices*). */
(function () {
  const TAB = "family_devices";
  const API = "/api/family_share/devices";
  let data = null;

  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const toast = (m) => (window.showToast ? showToast(m) : console.log(m));
  const when = (t) => (t ? new Date(t * 1000).toLocaleString() : "never");
  const SCOPE_LABEL = { personal: "my personal folder only", all: "everything my account can see" };

  /** @brief POST JSON to the devices API; throws with the server's message. */
  async function post(path, body) {
    const r = await fetch(API + path, { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || d.ok === false) throw new Error(d.error || ("HTTP " + r.status));
    return d;
  }

  const pane = () => document.getElementById("settings_pane_module_" + TAB);

  /** @brief Scope picker for a device row. */
  function scopeSel(cur) {
    return `<select class="fd-scope" title="What the phone sees of the library">${["personal", "all"].map((s) =>
      `<option value="${s}" ${cur === s ? "selected" : ""}>${esc(SCOPE_LABEL[s])}</option>`).join("")}</select>`;
  }

  /** @brief One device row: settings, pairing state and actions. */
  function row(dv) {
    const status = dv.paired
      ? (dv.last_ok ? `<span class="fs-ok">last seen ${esc(when(dv.last_ok))}</span>` : "paired, not seen yet")
      : `<span class="fs-err">not paired: paste the app's code</span>`;
    return `<tr data-id="${dv.id}">
      <td><input class="fd-name" value="${esc(dv.name)}"></td>
      <td><input class="fd-folder" value="${esc(dv.folder || "")}" placeholder="(my phone/${esc(dv.name)})"></td>
      <td>${scopeSel(dv.scope)}</td>
      <td><input class="fd-en" type="checkbox" ${dv.enabled ? "checked" : ""}></td>
      <td><input class="fd-code" placeholder="${dv.paired ? "paired - paste a new code to re-pair" : "paste the app's pairing code"}">
          <div class="fs-why">${status}${dv.fingerprint ? ` | key ${esc(dv.fingerprint)}` : ""}</div></td>
      <td class="fs-actions">
        <button class="fs-btn fs-btn-sm fd-save">Save</button>
        <button class="fs-btn fs-btn-sm fs-btn-ghost fd-key" title="The code you paste into the app">Code for the app</button>
        <button class="fs-btn fs-btn-sm fs-btn-ghost fd-rotate" title="Invalidate the key the app holds">Rotate</button>
        <button class="fs-btn fs-btn-sm fs-btn-danger fd-del">Remove</button></td></tr>`;
  }

  /** @brief Show the server's pairing code for a device under the table. */
  async function showCode(id, name, after) {
    const d = await post("/key", { id });
    const box = document.createElement("div"); box.className = "fs-keybox";
    box.innerHTML = `<div>Paste this into the app on <b>${esc(name)}</b> (Pair with server). It names this server
      <b>${esc(d.name)}</b>, carries its URL and public key (fingerprint <code>${esc(d.fingerprint)}</code>) and the
      secret the phone uses to reach it, so send it only to yourself.</div>
      <input readonly value="${esc(d.pairing_code)}"><button class="fs-btn fs-btn-sm">Copy</button>
      <button class="fs-btn fs-btn-sm fs-btn-ghost">Close</button>`;
    box.querySelector("input").addEventListener("focus", (e) => e.target.select());
    const [copy, close] = box.querySelectorAll("button");
    copy.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(d.pairing_code); toast("Copied"); } catch (_) { box.querySelector("input").select(); }
    });
    close.addEventListener("click", () => box.remove());
    after.after(box);
  }

  function render() {
    const p = pane(); if (!p) return;
    const me = data.me.display_name || data.me.username || "this account";
    const devs = data.devices;
    p.innerHTML = `<div class="fs-root">
      <section class="fs-card">
        <h3>My devices <small>phones running the CIM Family app, paired as <b>${esc(me)}</b></small></h3>
        ${data.inbound ? "" : `<p class="fs-err">Receiving from phones is switched off on this server (Family share options).</p>`}
        <table class="fs-table"><thead><tr><th>Name</th><th>Uploads land in</th><th>The phone sees</th><th>On</th>
          <th>Pairing</th><th></th></tr></thead>
          <tbody>${devs.map(row).join("")}
          <tr class="fs-new"><td><input class="fd-name" placeholder="${esc((data.me.username || "my") + "-phone")}"></td>
            <td><input class="fd-folder" placeholder="(my phone/&lt;name&gt;)"></td><td>${scopeSel("personal")}</td>
            <td><input class="fd-en" type="checkbox" checked></td><td></td>
            <td class="fs-actions"><button class="fs-btn fs-btn-sm fd-add">Add</button></td></tr></tbody></table>
        <p class="fs-help">A phone acts as your account: it can only see and fetch what you may see (<i>my personal folder
        only</i> confines it to your own folder), its photos are stored in your personal folder (or the folder you name,
        when you may write there) and count against your storage quota. If your account is disabled the phone stops
        working; if it is deleted, its phones are removed.</p>
        <p class="fs-help">Pairing: 1. add the phone here and click <b>Code for the app</b>; paste that code into the app
        (<a href="/static/app/cim-family.apk" download>download APK</a>). 2. The app then shows <i>its</i> code: paste it
        into the phone's row here and click Save. Compare the fingerprints the two sides show.</p>
        <p class="fs-help">This server: <b>${esc(data.server.name)}</b>, key <code>${esc(data.server.fingerprint)}</code>${
          data.server.my_url ? "" : `. <span class="fs-err">No public URL is set (an admin sets it in Family share), so the
          code carries none: type the server's address into the app.</span>`}</p>
      </section></div>`;
    p.querySelectorAll("tbody tr").forEach((tr) => {
      const id = Number(tr.dataset.id || 0);
      const q = (c) => tr.querySelector(c);
      const body = () => {
        const b = { id, name: q(".fd-name").value.trim() || q(".fd-name").placeholder, folder: q(".fd-folder").value,
                    scope: q(".fd-scope").value, enabled: q(".fd-en").checked };
        const code = q(".fd-code") ? q(".fd-code").value.trim() : "";
        if (code) b.pairing_code = code;
        return b;
      };
      const add = q(".fd-add"); if (add) add.addEventListener("click", async () => {
        try {
          const d = await post("/save", body());
          toast("Device added"); await load();
          const tr2 = pane().querySelector(`tr[data-id="${d.id}"]`);
          if (tr2) await showCode(d.id, d.peer.name, tr2.closest("table"));
        } catch (e) { toast(e.message); }
      });
      const save = q(".fd-save"); if (save) save.addEventListener("click", async () => {
        try { await post("/save", body()); toast("Device saved"); await load(); } catch (e) { toast(e.message); }
      });
      const key = q(".fd-key"); if (key) key.addEventListener("click", async () => {
        try { await showCode(id, q(".fd-name").value, tr.closest("table")); } catch (e) { toast(e.message); }
      });
      const rot = q(".fd-rotate"); if (rot) rot.addEventListener("click", async () => {
        if (!confirm("Rotate the key this phone uses? Paste the new code into the app afterwards.")) return;
        try { await post("/save", { ...body(), rotate_key_in: true }); toast("Rotated"); await load(); } catch (e) { toast(e.message); }
      });
      const del = q(".fd-del"); if (del) del.addEventListener("click", async () => {
        if (!confirm(`Remove '${q(".fd-name").value}'? The app stops working; photos already uploaded stay.`)) return;
        try { await post("/delete", { id }); await load(); } catch (e) { toast(e.message); }
      });
    });
    if (window.CIMFeatures && window.CIMFeatures.apply) window.CIMFeatures.apply(p);
  }

  /** @brief Fetch the account's devices and draw the tab. */
  async function load() {
    const p = pane(); if (!p) return;
    try {
      const r = await fetch(API);
      const d = await r.json().catch(() => ({}));
      if (!r.ok || d.ok === false) throw new Error(d.error || ("HTTP " + r.status));
      data = d;
    } catch (e) { p.innerHTML = `<p class="fs-err">${esc(e.message)}</p>`; return; }
    render();
  }

  document.addEventListener("module-settings-tab", (ev) => { if (ev.detail === TAB) load(); });
})();
