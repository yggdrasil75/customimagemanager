/* Family share — front-end.
 *
 * Renders the Settings → Family share tab (identity, peers, rules, options,
 * preview, outbox, received) into the pane the core created for this
 * module, and a small "who sees this?" badge in the viewer toggles. All
 * state lives on the server; this file only reads /api/family_share/* and
 * posts edits back. */
(function () {
  const ID = "family_share";
  const API = "/api/family_share";
  let state = null;

  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const toast = (m) => (window.showToast ? showToast(m) : console.log(m));
  const when = (t) => (t ? new Date(t * 1000).toLocaleString() : "—");

  async function get(path) {
    const r = await fetch(API + path);
    const d = await r.json().catch(() => ({}));
    if (!r.ok || d.ok === false) throw new Error(d.error || ("HTTP " + r.status));
    return d;
  }
  async function post(path, body) {
    const r = await fetch(API + path, { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || d.ok === false) throw new Error(d.error || ("HTTP " + r.status));
    return d;
  }
  // Option edits are buffered here and written by the modal's Save button
  // (persistFamilyShare below), like the core panes; closing without Save
  // discards them. Peers and rules are records with their own Save/Add.
  let pending = {};
  function setOption(key, value) {
    pending[key] = value;
    if (state) state.options[key] = value;
  }
  window.persistFamilyShare = async function () {
    const keys = Object.keys(pending);
    if (!keys.length) return { ok: true };
    const body = Object.assign({}, pending);
    try {
      const r = await fetch("/api/update_settings", { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (!r.ok) return { ok: false, error: "Family share settings failed to save" };
    } catch (e) { return { ok: false, error: "Family share settings failed to save" }; }
    pending = {};
    return { ok: true };
  };

  const pane = () => document.getElementById("settings_pane_module_" + ID);

  // ── layout ────────────────────────────────────────────────────────────
  function shell() {
    const p = pane(); if (!p) return null;
    if (!p.querySelector(".fs-root")) {
      p.innerHTML = `
      <div class="fs-root">
        <p class="fs-help">Photos leave this instance only when a <b>share</b> rule for a peer matches
        them and no <b>block</b> rule vetoes it. Use <b>Preview</b> to see exactly what a peer would
        get before trusting a rule set. Received photos land under the incoming folder and are
        never re-shared unless you turn that on.</p>
        <div class="fs-grid">
          <section class="fs-card" id="fs_identity"></section>
          <section class="fs-card" id="fs_options"></section>
        </div>
        <section class="fs-card" id="fs_peers"></section>
        <section class="fs-card" id="fs_rules"></section>
        <section class="fs-card" id="fs_preview"></section>
        <section class="fs-card" id="fs_outbox"></section>
        <section class="fs-card" id="fs_received"></section>
      </div>`;
    }
    return p;
  }

  async function load() {
    const p = shell(); if (!p) return;
    try { state = await get("/state"); Object.assign(state.options, pending); }
    catch (e) { p.querySelector(".fs-root").innerHTML = `<p class="fs-err">${esc(e.message)}</p>`; return; }
    renderIdentity(); renderOptions(); renderPeers(); renderRules(); renderPreview();
    renderOutbox(); renderReceived();
  }

  // ── identity ──────────────────────────────────────────────────────────
  function renderIdentity() {
    const el = document.getElementById("fs_identity");
    el.innerHTML = `<h3>This instance</h3>
      <label class="fs-row"><span>Name peers know me by</span>
        <input id="fs_name" value="${esc(state.options.family_share_name || "")}"
               placeholder="${esc(state.instance.name)}"></label>
      <label class="fs-row"><span>My URL (goes into pairing codes)</span>
        <input id="fs_myurl" value="${esc(state.options.family_share_my_url || "")}"
               placeholder="https://my-box.example:8000"></label>
      <div class="fs-row"><span>Instance id</span><code>${esc(state.instance.id)}</code></div>
      <div class="fs-row" title="Compare this with what the other side sees after pairing"><span>My key fingerprint</span>
        <code>${esc(state.instance.fingerprint)}</code></div>
      <p class="fs-help">🔒 Every photo and its metadata is end-to-end encrypted to the peer's pinned public key
      (X25519 + AES-256-GCM). Routers, ISPs and anyone on the wifi only ever see ciphertext.</p>
      <div class="fs-row"><span>Outbox</span><span>${
        ["pending", "sent", "revoke", "error"].map((k) => `${k}: ${state.outbox[k] || 0}`).join(" · ")
      }</span></div>
      <div class="fs-row"><span>Last plan</span><span>${when(state.last_plan_at)}${
        state.last_plan ? ` (desired ${state.last_plan.desired}, new ${state.last_plan.new}, changed ${state.last_plan.changed}, revoke ${state.last_plan.revoke})` : ""}${
        state.dirty ? " · re-plan pending" : ""}</span></div>
      <div class="fs-actions"><button id="fs_sync" class="fs-btn">Plan &amp; sync now</button>
        <button id="fs_reload" class="fs-btn fs-btn-ghost">Refresh</button></div>`;
    el.querySelector("#fs_myurl").addEventListener("change", (ev) => setOption("family_share_my_url", ev.target.value.trim()));
    el.querySelector("#fs_name").addEventListener("change", (ev) => setOption("family_share_name", ev.target.value.trim()));
    el.querySelector("#fs_sync").addEventListener("click", async () => {
      try { await post("/sync"); toast("Planning…"); setTimeout(load, 1500); } catch (e) { toast(e.message); }
    });
    el.querySelector("#fs_reload").addEventListener("click", load);
  }

  // ── options ───────────────────────────────────────────────────────────
  const OPTS = [
    ["family_share_outbound", "toggle", "Send to peers", "Master switch for everything leaving this instance."],
    ["family_share_inbound", "toggle", "Accept from peers", "Master switch for everything peers push here."],
    ["family_share_incoming_folder", "text", "Incoming folder", "Received photos land in <folder>/<peer name>/<their folder>."],
    ["family_share_tag_received", "text", "Tag received photos", "{peer} becomes the peer's name. Empty = no tag."],
    ["revoke_on_unshare", "toggle", "Revoke when a photo stops matching", "Rule edited, tag removed, moved out of a shared folder → peers are told to remove it."],
    ["revoke_on_delete", "toggle", "Revoke when I delete a photo", "Deleting here also removes the copies you shared."],
    ["honor_revoke", "toggle", "Honour revokes from peers", "When a peer revokes, delete the copy they gave me."],
    ["reshare_received", "toggle", "Re-share photos I received", "OFF keeps a cousin's photo from travelling on through my rules."],
    ["match_unconfirmed_tags", "toggle", "Tag rules match unconfirmed (AI) tags", "OFF: only tags you confirmed can trigger a share."],
    ["share_all_albums", "toggle", "Send all album names", "OFF: only the album a share rule names is sent along."],
    ["share_regions", "toggle", "Send regions (people boxes / names)", ""],
    ["family_share_interval_min", "number", "Full re-plan every (minutes)", "Edits plan immediately; this catches anything missed."],
  ];
  function renderOptions() {
    const el = document.getElementById("fs_options");
    el.innerHTML = `<h3>Options <small>applied when you click Save below</small></h3>` + OPTS.map(([k, kind, label, help]) => {
      const v = state.options[k];
      const ctl = kind === "toggle"
        ? `<input type="checkbox" data-opt="${k}" ${v ? "checked" : ""}>`
        : `<input type="${kind}" data-opt="${k}" value="${esc(v == null ? "" : v)}" ${kind === "number" ? 'min="1"' : ""}>`;
      return `<label class="fs-row" title="${esc(help)}"><span>${esc(label)}</span>${ctl}</label>`;
    }).join("");
    el.querySelectorAll("[data-opt]").forEach((inp) => inp.addEventListener("change", () => {
      const k = inp.dataset.opt;
      setOption(k, inp.type === "checkbox" ? inp.checked : (inp.type === "number" ? Number(inp.value) : inp.value));
    }));
  }

  // ── peers ─────────────────────────────────────────────────────────────
  function renderPeers() {
    const el = document.getElementById("fs_peers");
    const kindSel = (k) => `<select class="fs-p-kind"><option value="peer" ${k !== "device" ? "selected" : ""}>family</option>
        <option value="device" ${k === "device" ? "selected" : ""}>my phone</option></select>`;
    const rows = state.peers.map((p) => `
      <tr data-id="${p.id}">
        <td>${kindSel(p.kind)}</td>
        <td><input class="fs-p-name" value="${esc(p.name)}">
            <input class="fs-p-folder" value="${esc(p.folder || "")}" placeholder="uploads land in (phone/${esc(p.name)})" ${p.kind === "device" ? "" : "hidden"}></td>
        <td><input class="fs-p-url" value="${esc(p.url)}" placeholder="https://their-box:5000"></td>
        <td><input class="fs-p-key" placeholder="${p.has_key_out && p.pub_key ? "paired" : "paste their pairing code"}">
            ${p.pub_key ? `<div class="fs-fp" title="Their key fingerprint — compare with what they see">🔒 ${esc(p.fingerprint)}</div>`
                        : `<div class="fs-err">no key pinned — nothing will be sent</div>`}</td>
        <td><input class="fs-p-en" type="checkbox" ${p.enabled ? "checked" : ""}></td>
        <td class="fs-status">${p.last_error ? `<span class="fs-err" title="${esc(p.last_error)}">error</span>`
          : (p.last_ok ? `<span class="fs-ok" title="${when(p.last_ok)}">ok</span>` : "—")}</td>
        <td class="fs-actions">
          <button class="fs-btn fs-btn-sm fs-p-save">Save</button>
          <button class="fs-btn fs-btn-sm fs-btn-ghost fs-p-test">Test</button>
          <button class="fs-btn fs-btn-sm fs-btn-ghost fs-p-showkey" title="One string they paste on their instance: my name, URL, public key and the secret they use to reach me">Pairing code for them</button>
          <button class="fs-btn fs-btn-sm fs-btn-ghost fs-p-rotate" title="Invalidate the key they hold">Rotate</button>
          <button class="fs-btn fs-btn-sm fs-btn-danger fs-p-del">Remove</button>
        </td></tr>`).join("");
    el.innerHTML = `<h3>Peers <small>other instances; both sides must add each other</small></h3>
      <table class="fs-table"><thead><tr><th>Kind</th><th>Name</th><th>URL</th><th>Their pairing code / key</th><th>On</th><th>Status</th><th></th></tr></thead>
      <tbody>${rows}
        <tr class="fs-new"><td>${kindSel("peer")}</td>
          <td><input class="fs-p-name" placeholder="mom / my-phone"><input class="fs-p-folder" placeholder="uploads land in" hidden></td>
          <td><input class="fs-p-url" placeholder="https://their-box:5000"></td>
          <td><input class="fs-p-key" placeholder="paste their pairing code (or add now, pair later)"></td>
          <td><input class="fs-p-en" type="checkbox" checked></td><td></td>
          <td class="fs-actions"><button class="fs-btn fs-btn-sm fs-p-save">Add</button></td></tr>
      </tbody></table>
      <p class="fs-help"><b>My phone</b> peers are your own devices running the CIM Family app
      (<a href="/static/app/cim-family.apk" download>download APK</a>, built with the docker image). Their uploads are your own
      photos: they land in the folder above, get no "from:" tag and flow to family through the rules like anything else.
      Pair the same way — the app shows its pairing code, and you paste this instance's code into the app.</p>
      <p class="fs-help">Set-up: add the peer here (name only is fine), click <b>Pairing code for them</b> and send them
      the string. They paste it in this box on their instance, which creates/pairs the peer entry for you, then they send
      you <i>their</i> pairing code, which you paste here. <b>Test</b> checks the connection and that the pinned key
      matches the instance at that URL. Compare fingerprints out-of-band (a phone call) if you want to be sure nobody
      swapped a code in transit.</p>`;

    el.querySelectorAll("tr").forEach((tr) => {
      const id = Number(tr.dataset.id || 0);
      const q = (c) => tr.querySelector(c);
      const kind = q(".fs-p-kind"); if (kind) kind.addEventListener("change", () => { q(".fs-p-folder").hidden = kind.value !== "device"; });
      const save = q(".fs-p-save"); if (save) save.addEventListener("click", async () => {
        try {
          const raw = q(".fs-p-key").value.trim();
          const body = { id, name: q(".fs-p-name").value, url: q(".fs-p-url").value, enabled: q(".fs-p-en").checked,
                         kind: q(".fs-p-kind").value, folder: q(".fs-p-folder").value };
          if (raw.startsWith("fs1.")) body.pairing_code = raw; else if (raw) body.key_out = raw;
          await post("/peers/save", body);
          toast(id ? "Peer saved" : "Peer added"); await load();
        } catch (e) { toast(e.message); }
      });
      const test = q(".fs-p-test"); if (test) test.addEventListener("click", async () => {
        try { const d = await post("/peers/test", { id });
          toast(`Reached ${d.peer.name} · fingerprint ${d.peer.fingerprint}${d.pinned ? " · matches pinned key" : " · NOT PINNED: paste their pairing code"}`);
          await load(); }
        catch (e) { toast("Test failed: " + e.message); await load(); }
      });
      const show = q(".fs-p-showkey"); if (show) show.addEventListener("click", async () => {
        try {
          const d = await post("/peers/key", { id });
          const box = document.createElement("div"); box.className = "fs-keybox";
          box.innerHTML = `<div>Send this to <b>${esc(q(".fs-p-name").value)}</b>; they paste it into the pairing box
            on their instance. It names me <b>${esc(d.name)}</b>, carries my URL, my public key
            (fingerprint <code>${esc(d.fingerprint)}</code>) and the secret they use to reach me.</div>
            <input readonly value="${esc(d.pairing_code)}"><button class="fs-btn fs-btn-sm">Copy</button>
            <button class="fs-btn fs-btn-sm fs-btn-ghost">Close</button>`;
          d.key_in = d.pairing_code;
          box.querySelector("input").addEventListener("focus", (e) => e.target.select());
          box.querySelectorAll("button")[0].addEventListener("click", async () => {
            try { await navigator.clipboard.writeText(d.key_in); toast("Copied"); } catch (_) { box.querySelector("input").select(); }
          });
          box.querySelectorAll("button")[1].addEventListener("click", () => box.remove());
          tr.parentNode.parentNode.after(box);
        } catch (e) { toast(e.message); }
      });
      const rot = q(".fs-p-rotate"); if (rot) rot.addEventListener("click", async () => {
        if (!confirm("Rotate the key this peer uses to reach me? They will need the new one.")) return;
        try { await post("/peers/save", { id, name: q(".fs-p-name").value, url: q(".fs-p-url").value,
          enabled: q(".fs-p-en").checked, kind: q(".fs-p-kind").value, folder: q(".fs-p-folder").value,
          rotate_key_in: true }); toast("Rotated"); await load(); }
        catch (e) { toast(e.message); }
      });
      const del = q(".fs-p-del"); if (del) del.addEventListener("click", async () => {
        if (!confirm(`Remove peer '${q(".fs-p-name").value}'? Nothing is revoked on their side.`)) return;
        try { await post("/peers/delete", { id }); await load(); } catch (e) { toast(e.message); }
      });
    });
  }

  // ── rules ─────────────────────────────────────────────────────────────
  function peerChecks(sel) {
    return state.peers.map((p) => `<label class="fs-chk"><input type="checkbox" value="${p.id}"
      ${sel.includes(p.id) ? "checked" : ""}> ${esc(p.name)}</label>`).join("") || "<i>add a peer first</i>";
  }
  function valueInput(kind, value) {
    const list = kind === "folder" ? "fs_dl_folders" : (kind === "album" ? "fs_dl_albums" : "");
    return `<input class="fs-r-value" value="${esc(value)}" ${list ? `list="${list}"` : ""}
      placeholder="${kind === "folder" ? "trips/2026 (empty = whole library)" : kind === "album" ? "Beach 2026" : "family"}">`;
  }
  function ruleRow(r) {
    const isNew = !r.id;
    return `<tr data-id="${r.id || 0}" class="${isNew ? "fs-new" : ""} fs-mode-${r.mode}">
      <td><select class="fs-r-mode"><option value="share" ${r.mode === "share" ? "selected" : ""}>share</option>
          <option value="block" ${r.mode === "block" ? "selected" : ""}>block</option></select></td>
      <td><select class="fs-r-kind">${["folder", "album", "tag"].map((k) =>
        `<option value="${k}" ${r.kind === k ? "selected" : ""}>${k}</option>`).join("")}</select></td>
      <td class="fs-r-valcell">${valueInput(r.kind, r.value)}
        <label class="fs-chk fs-r-reccell" ${r.kind === "folder" ? "" : "hidden"}>
          <input class="fs-r-rec" type="checkbox" ${r.recursive ? "checked" : ""}> incl. subfolders</label></td>
      <td class="fs-r-peers">${peerChecks(r.peers)}
        <div class="fs-help fs-r-blockhint" ${r.mode === "block" ? "" : "hidden"}>block with none ticked = everyone</div></td>
      <td><input class="fs-r-name" value="${esc(r.name)}" placeholder="note"></td>
      <td><input class="fs-r-en" type="checkbox" ${r.enabled ? "checked" : ""}></td>
      <td class="fs-actions"><button class="fs-btn fs-btn-sm fs-r-save">${isNew ? "Add" : "Save"}</button>
        ${isNew ? "" : '<button class="fs-btn fs-btn-sm fs-btn-danger fs-r-del">Delete</button>'}</td></tr>`;
  }
  function renderRules() {
    const el = document.getElementById("fs_rules");
    const rules = state.rules.slice().sort((a, b) => (a.mode === b.mode ? a.id - b.id : (a.mode === "block" ? -1 : 1)));
    el.innerHTML = `<h3>Rules <small>share = allow to these peers · block = never to these peers (block wins)</small></h3>
      <datalist id="fs_dl_folders">${state.folders.map((f) => `<option value="${esc(f)}">`).join("")}</datalist>
      <datalist id="fs_dl_albums">${state.albums.map((a) => `<option value="${esc(a)}">`).join("")}</datalist>
      <table class="fs-table"><thead><tr><th>Mode</th><th>Kind</th><th>Value</th><th>Peers</th><th>Note</th><th>On</th><th></th></tr></thead>
      <tbody>${rules.map(ruleRow).join("")}${ruleRow({ mode: "share", kind: "album", value: "", recursive: 1, peers: [], name: "", enabled: 1 })}</tbody></table>
      <p class="fs-help">Examples — <i>share album "Beach 2026" → sister, cousins</i>; <i>share tag "family" → mom</i>;
      <i>block folder "work" → everyone</i>; <i>block tag "sister" → cousins</i>.</p>`;
    el.querySelectorAll("tbody tr").forEach((tr) => {
      const id = Number(tr.dataset.id || 0);
      const q = (c) => tr.querySelector(c);
      q(".fs-r-kind").addEventListener("change", () => {
        const kind = q(".fs-r-kind").value;
        const old = q(".fs-r-value");
        old.insertAdjacentHTML("afterend", valueInput(kind, "")); old.remove();
        q(".fs-r-reccell").hidden = kind !== "folder";
      });
      q(".fs-r-mode").addEventListener("change", () => {
        tr.className = tr.className.replace(/fs-mode-\w+/, "fs-mode-" + q(".fs-r-mode").value);
        q(".fs-r-blockhint").hidden = q(".fs-r-mode").value !== "block";
      });
      q(".fs-r-save").addEventListener("click", async () => {
        const peers = [...tr.querySelectorAll(".fs-r-peers input:checked")].map((i) => Number(i.value));
        try {
          await post("/rules/save", { id, mode: q(".fs-r-mode").value, kind: q(".fs-r-kind").value,
            value: q(".fs-r-value").value, recursive: q(".fs-r-rec").checked, peers,
            name: q(".fs-r-name").value, enabled: q(".fs-r-en").checked });
          toast(id ? "Rule saved" : "Rule added"); await load();
        } catch (e) { toast(e.message); }
      });
      const del = q(".fs-r-del"); if (del) del.addEventListener("click", async () => {
        if (!confirm("Delete this rule? Photos it alone allowed will be revoked from peers (if revoke-on-unshare is on).")) return;
        try { await post("/rules/delete", { id }); await load(); } catch (e) { toast(e.message); }
      });
    });
  }

  // ── preview ───────────────────────────────────────────────────────────
  function renderPreview() {
    const el = document.getElementById("fs_preview");
    el.innerHTML = `<h3>Preview <small>what a peer would have, under the current rules</small></h3>
      <div class="fs-actions"><select id="fs_pv_peer">${state.peers.map((p) =>
        `<option value="${p.id}">${esc(p.name)}</option>`).join("")}</select>
        <button id="fs_pv_go" class="fs-btn">Show</button><span id="fs_pv_n"></span></div>
      <div id="fs_pv_list" class="fs-list"></div>`;
    el.querySelector("#fs_pv_go").addEventListener("click", async () => {
      const pid = el.querySelector("#fs_pv_peer").value; if (!pid) return;
      const list = el.querySelector("#fs_pv_list"); list.textContent = "…";
      try {
        const d = await get(`/preview?peer_id=${pid}&limit=1000`);
        el.querySelector("#fs_pv_n").textContent = `${d.total} file${d.total === 1 ? "" : "s"}`;
        list.innerHTML = d.files.length ? d.files.map((f) =>
          `<div class="fs-item"><a href="#" data-open="${esc(f.rel_path)}">${esc(f.rel_path)}</a>
            <span class="fs-why">${esc(f.reasons.join("; "))}${f.albums.length ? " · albums: " + esc(f.albums.join(", ")) : ""}</span></div>`).join("")
          : "<i>nothing would be shared with this peer</i>";
        list.querySelectorAll("[data-open]").forEach((a) => a.addEventListener("click", (ev) => {
          ev.preventDefault(); if (window.selectFile) selectFile(a.dataset.open);
        }));
      } catch (e) { list.innerHTML = `<span class="fs-err">${esc(e.message)}</span>`; }
    });
  }

  // ── outbox / received ─────────────────────────────────────────────────
  async function renderOutbox() {
    const el = document.getElementById("fs_outbox");
    el.innerHTML = `<h3>Outbox <small>what is waiting to go, what failed</small></h3>
      <div class="fs-actions"><select id="fs_ob_status"><option value="">all</option>
        ${["pending", "error", "revoke", "sent"].map((s) => `<option value="${s}">${s}</option>`).join("")}</select>
        <button id="fs_ob_go" class="fs-btn fs-btn-ghost">Show</button>
        <button id="fs_ob_retry" class="fs-btn fs-btn-ghost">Retry failed</button></div>
      <div id="fs_ob_list" class="fs-list"></div>`;
    const show = async () => {
      const st = el.querySelector("#fs_ob_status").value;
      const list = el.querySelector("#fs_ob_list"); list.textContent = "…";
      try {
        const d = await get(`/outbox?status=${st}&limit=300`);
        list.innerHTML = d.rows.length ? d.rows.map((r) =>
          `<div class="fs-item"><span class="fs-tag fs-st-${r.status}">${r.status}</span> ${esc(r.rel_path)}
           → <b>${esc(r.peer)}</b> <span class="fs-why">${when(r.updated)}${r.attempts ? ` · attempts ${r.attempts}` : ""}${r.error ? " · " + esc(r.error) : ""}</span></div>`).join("")
          : "<i>empty</i>";
      } catch (e) { list.innerHTML = `<span class="fs-err">${esc(e.message)}</span>`; }
    };
    el.querySelector("#fs_ob_go").addEventListener("click", show);
    el.querySelector("#fs_ob_retry").addEventListener("click", async () => {
      try { await post("/outbox/retry"); toast("Retrying"); show(); } catch (e) { toast(e.message); }
    });
    if ((state.outbox.pending || 0) + (state.outbox.error || 0) + (state.outbox.revoke || 0)) show();
  }
  async function renderReceived() {
    const el = document.getElementById("fs_received");
    el.innerHTML = `<h3>Received <small>${state.received} photo${state.received === 1 ? "" : "s"} from peers</small></h3>
      <div class="fs-actions"><button id="fs_rc_go" class="fs-btn fs-btn-ghost">Show recent</button></div>
      <div id="fs_rc_list" class="fs-list"></div>`;
    el.querySelector("#fs_rc_go").addEventListener("click", async () => {
      const list = el.querySelector("#fs_rc_list"); list.textContent = "…";
      try {
        const d = await get("/received?limit=300");
        list.innerHTML = d.rows.length ? d.rows.map((r) =>
          `<div class="fs-item"><b>${esc(r.peer)}</b> → ${r.rel_path ? esc(r.rel_path) : (r.queue_id ? "<i>still ingesting</i>" : "<i>deleted here (declined)</i>")}
           <span class="fs-why">${when(r.received)}${r.duplicate ? " · already had it" : ""}</span></div>`).join("")
          : "<i>nothing yet</i>";
      } catch (e) { list.innerHTML = `<span class="fs-err">${esc(e.message)}</span>`; }
    });
  }

  document.addEventListener("module-settings-tab", (ev) => { if (ev.detail === ID) load(); });
  // A fresh open of the settings modal starts from what the server has.
  const _origOpen = window.refreshModuleSettings;
  if (typeof _origOpen === "function") window.refreshModuleSettings = async function () { pending = {}; return _origOpen.apply(this, arguments); };

  // ── viewer badge: who sees the current photo ──────────────────────────
  if (window.registerControlButton) {
    registerControlButton("viewer_toggles",
      '<button id="fs_badge" class="fs-badge" title="Family share: who sees this photo" hidden>👪</button>');
  }
  let badgeFile = null;
  if (window.registerFileMetaHook) registerFileMetaHook(async (meta, filename) => {
    badgeFile = filename;
    const b = document.getElementById("fs_badge"); if (!b) return;
    b.hidden = true;
    try {
      const d = await get(`/file?rel_path=${encodeURIComponent(filename)}`);
      if (badgeFile !== filename) return;
      const shared = d.peers.filter((p) => p.shared).map((p) => p.peer + (p.status && p.status !== "sent" ? ` (${p.status})` : ""));
      const parts = [];
      if (d.received_from) parts.push("from " + d.received_from);
      if (shared.length) parts.push("→ " + shared.join(", "));
      b.textContent = "👪 " + (parts.join(" · ") || "not shared");
      b.title = d.peers.map((p) => `${p.peer}: ${p.shared ? "shared" : "not shared"}${p.reasons.length ? " — " + p.reasons.join("; ") : ""}`).join("\n") || "no peers";
      b.classList.toggle("fs-badge-on", shared.length > 0);
      b.hidden = !(d.peers.length || d.received_from);
    } catch (_) { /* module off or no permission: stay hidden */ }
  });
})();
