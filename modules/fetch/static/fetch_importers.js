/* Library importers (Immich, Google Takeout, Apple / iCloud) - the settings-tab
 * UI they share. It belongs to the fetch module because importers ARE fetchers:
 * "Import now" queues a fetch job and "every N hours" is a fetch watch.
 *
 * An importer's own script calls ImportKit.mount({...}) with its fields. The
 * tab lists the importer's SOURCES (an account, a folder), each with its
 * schedule, last run and progress, and a form to add or edit one. Saving is an
 * action (it isn't tied to the settings modal's Save button). A source that
 * needs one more value to sign in (Apple's 2FA code) gets an inline prompt. */
(function () {
  if (window.ImportKit) return;

  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const toast = (m) => (window.showToast ? showToast(m) : console.log(m));
  const mb = (b) => (b >= 1 << 30 ? (b / (1 << 30)).toFixed(1) + " GB" : (b / (1 << 20)).toFixed(1) + " MB");
  const when = (t) => (t ? new Date(t * 1000).toLocaleString() : "");

  async function call(url, body) {
    const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" },
                                            body: JSON.stringify(body) };
    const r = await fetch(url, opt);
    const d = await r.json().catch(() => ({}));
    if (!r.ok || d.ok === false || d.success === false) throw new Error(d.error || ("HTTP " + r.status));
    return d;
  }

  const COMMON = [
    { key: "every_h", label: "Check for new photos every (hours)", kind: "number", default: 0,
      help: "0 = only when you click Import now. Each run only fetches what's new." },
    { key: "favorite_tag", label: "Tag favourites as", kind: "text", default: "favorite", help: "empty = don't tag" },
    { key: "archived_tag", label: "Tag archived as", kind: "text", default: "archived" },
    { key: "hidden_tag", label: "Tag hidden as", kind: "text", default: "hidden" },
    { key: "source_tag", label: "Also tag every imported file", kind: "text", default: "", help: "e.g. import:immich" },
    { key: "overwrite_dates", label: "Replace dates already in the file", kind: "toggle", default: false,
      help: "Off: a camera's own EXIF date wins; the service's date is used only when the file has none." },
  ];

  function fieldHtml(f, cfg, hasSecrets) {
    const v = cfg && f.key in cfg ? cfg[f.key] : f.default;
    const help = f.help ? `<div class="ik-help">${esc(f.help)}</div>` : "";
    let ctl;
    if (f.kind === "select") {
      ctl = `<select data-k="${f.key}">${f.options.map(([o, l]) =>
        `<option value="${esc(o)}" ${String(v) === String(o) ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>`;
    } else if (f.kind === "toggle") {
      ctl = `<input type="checkbox" data-k="${f.key}" ${v ? "checked" : ""}>`;
    } else if (f.kind === "path") {
      ctl = `<select data-k="${f.key}" class="ik-path"><option value="${esc(v || "")}">${esc(v || "- choose -")}</option></select>`;
    } else {
      const secret = f.secret;
      ctl = `<input type="${secret ? "password" : f.kind === "number" ? "number" : "text"}" data-k="${f.key}"
               ${secret ? 'data-secret="1" autocomplete="new-password"' : ""}
               value="${secret ? "" : esc(v == null ? "" : v)}"
               placeholder="${esc(secret && hasSecrets ? "•••• stored (leave blank to keep)" : (f.placeholder || ""))}">`;
    }
    return `<label class="ik-row"><span>${esc(f.label)}</span>${ctl}</label>${help}`;
  }

  function readForm(box) {
    const config = {}, secrets = {};
    box.querySelectorAll("[data-k]").forEach((el) => {
      const v = el.type === "checkbox" ? el.checked : (el.type === "number" ? Number(el.value) : el.value);
      (el.dataset.secret ? secrets : config)[el.dataset.k] = v;
    });
    return { config, secrets };
  }

  function jobLine(j) {
    if (!j) return `<span class="ik-dim">never run</span>`;
    const st = { pending: "queued", downloading: "running" }[j.status] || j.status;
    const pct = j.total ? Math.min(100, Math.round((100 * j.downloaded) / j.total)) : null;
    return `<span class="ik-st ik-st-${esc(st)}">${esc(st)}</span>
      <span class="ik-dim">${esc(when(j.updated))}</span>
      ${pct !== null && (j.status === "downloading") ? `<div class="ik-bar"><div style="width:${pct}%"></div></div>` : ""}
      <div class="ik-dim">${j.downloaded}${j.total ? " / " + j.total : ""} handed to ingest${j.message ? " | " + esc(j.message) : ""}</div>
      ${j.error ? `<div class="ik-err">${esc(j.error)}</div>` : ""}`;
  }

  function mount(spec) {
    const paneId = "settings_pane_module_" + spec.tab;
    const api = "/api/import/" + spec.fetcher;
    let timer = null, editing = null, state = null;
    const fields = () => (spec.fields || []).concat(spec.common === false ? [] : COMMON);

    function pane() { return document.getElementById(paneId); }

    async function render() {
      const p = pane(); if (!p) return;
      if (!p.querySelector(".ik-root")) {
        p.innerHTML = `<div class="ik-root"><h3>${esc(spec.title)}</h3>
          <div class="ik-intro">${spec.intro || ""}</div>
          <div class="ik-card"><b>${esc(spec.sourcesLabel || "Sources")}</b><div class="ik-sources"></div></div>
          <div class="ik-card ik-form"></div></div>`;
        p.querySelector(".ik-sources").addEventListener("click", onClick);
      }
      await refresh();
      renderForm();
    }

    async function renderForm(src) {
      editing = src || null;
      const box = pane().querySelector(".ik-form");
      const cfg = src ? src.config : {};
      box.innerHTML = `<b>${src ? "Edit " + esc(src.label) : esc(spec.addLabel || "Add a source")}</b>
        ${fields().map((f) => fieldHtml(f, cfg, src && src.has_secrets)).join("")}
        <div class="ik-actions"><button class="ik-btn" data-save="run">Save &amp; import now</button>
          <button class="ik-btn ik-btn-ghost" data-save="save">Save</button>
          ${src ? '<button class="ik-btn ik-btn-ghost" data-save="cancel">Cancel</button>' : ""}</div>
        <div class="ik-prompt" hidden></div>`;
      box.querySelectorAll("[data-save]").forEach((b) => b.addEventListener("click", () => save(b.dataset.save)));
      if (spec.fields.some((f) => f.kind === "path")) fillPaths(box, cfg);
    }

    async function fillPaths(box, cfg) {
      try {
        const d = await call(api + "/browse");
        box.querySelectorAll(".ik-path").forEach((sel) => {
          const cur = cfg[sel.dataset.k] || "";
          sel.innerHTML = `<option value="">${d.exists ? "- choose -" : "import folder missing: " + esc(d.root)}</option>` +
            d.entries.map((e) => `<option value="${esc(e.name)}" ${e.name === cur ? "selected" : ""}>
              ${e.kind === "zip" ? "🗜" : "📁"} ${esc(e.name)}${e.size ? " (" + mb(e.size) + ")" : ""}</option>`).join("");
        });
      } catch (e) { toast(e.message); }
    }

    async function save(mode) {
      if (mode === "cancel") { renderForm(); return; }
      const box = pane().querySelector(".ik-form");
      const body = { id: editing ? editing.id : 0, ...readForm(box), run_now: mode === "run" };
      try {
        const d = await call(api + "/save", body);
        if (d.prompt) { showPrompt(box, d.id, d.prompt); }
        else { toast(spec.title + (mode === "run" ? ": import started" : ": saved")); renderForm(); }
        refresh();
      } catch (e) { toast(e.message); }
    }

    function showPrompt(box, id, prompt) {
      const pr = box.querySelector(".ik-prompt");
      pr.hidden = false;
      pr.innerHTML = `<div>${esc(prompt.message || "")}</div>
        <label class="ik-row"><span>${esc(prompt.label)}</span><input data-prompt autocomplete="one-time-code"></label>
        <div class="ik-actions"><button class="ik-btn">${esc(prompt.button || "Continue")}</button></div>`;
      pr.querySelector("button").addEventListener("click", async () => {
        const value = pr.querySelector("[data-prompt]").value.trim();
        try {
          const d = await call(api + "/action", { id, action: prompt.action, [prompt.field]: value });
          if (d.prompt) { showPrompt(box, id, d.prompt); return; }
          pr.hidden = true;
          toast(d.message || "Signed in");
          renderForm(); refresh();
        } catch (e) { toast(e.message); }
      });
      pr.querySelector("[data-prompt]").focus();
    }

    async function refresh() {
      clearTimeout(timer);
      const p = pane(); if (!p || !p.querySelector(".ik-sources")) return;
      try { state = await call(api + "/state"); } catch (e) { return; }
      const list = p.querySelector(".ik-sources");
      list.innerHTML = state.sources.length ? state.sources.map((s) => {
        const j = s.jobs[0];
        const active = j && (j.status === "pending" || j.status === "downloading");
        const w = s.watch;
        const led = s.ledger || {};
        return `<div class="ik-src" data-id="${s.id}">
          <div><b>${esc(s.label)}</b> ${s.status ? `<span class="ik-st ik-st-error">${esc(s.status)}</span>` : ""}
            <span class="ik-dim">${w && w.enabled ? "every " + w.every_h + " h" : "manual"}
            | ${led.done || 0} imported${led.failed ? `, <span class="ik-err">${led.failed} failed</span>` : ""}
            ${led.queued ? ", " + led.queued + " ingesting" : ""}</span></div>
          ${jobLine(j)}
          ${spec.sourceExtra ? spec.sourceExtra(s) : ""}
          <div class="ik-actions">
            ${s.status ? `<button class="ik-btn ik-btn-sm" data-act="edit">Sign in again</button>` :
              active ? `<button class="ik-btn ik-btn-sm ik-btn-ghost" data-act="stop" data-job="${j.id}">Stop</button>`
                     : `<button class="ik-btn ik-btn-sm" data-act="run">Import now</button>`}
            ${led.failed ? `<button class="ik-btn ik-btn-sm ik-btn-ghost" data-act="retry">Retry failed</button>
                            <button class="ik-btn ik-btn-sm ik-btn-ghost" data-act="failures">Failures</button>` : ""}
            <button class="ik-btn ik-btn-sm ik-btn-ghost" data-act="edit">Edit</button>
            <button class="ik-btn ik-btn-sm ik-btn-ghost" data-act="delete">Remove</button></div>
          <div class="ik-failures" hidden></div></div>`;
      }).join("") : `<div class="ik-dim">None yet - add one below.</div>`;
      const live = state.sources.some((s) => s.jobs[0] && ["pending", "downloading"].includes(s.jobs[0].status));
      if (p.offsetParent !== null) timer = setTimeout(refresh, live ? 2500 : 15000);
    }

    async function onClick(ev) {
      const b = ev.target.closest("[data-act]"); if (!b) return;
      const id = Number(b.closest(".ik-src").dataset.id), act = b.dataset.act;
      const src = state.sources.find((s) => s.id === id);
      try {
        if (act === "edit") { renderForm(src); pane().querySelector(".ik-form").scrollIntoView({ block: "nearest" }); return; }
        if (act === "run") await call(api + "/run", { id });
        if (act === "retry") await call(api + "/run", { id, retry_failed: true });
        if (act === "stop") await call("/api/fetch/cancel", { id: Number(b.dataset.job) });
        if (act === "delete") {
          if (!confirm(`Remove ${src.label}? Imported photos stay in the library.`)) return;
          await call(api + "/delete", { id });
        }
        if (act === "failures") {
          const box = b.closest(".ik-src").querySelector(".ik-failures");
          if (!box.hidden) { box.hidden = true; return; }
          const d = await call(api + "/failures?id=" + id);
          box.hidden = false;
          box.innerHTML = d.rows.map((r) => `<div class="ik-fail"><b>${esc(r.name || r.item_key)}</b>
            <span class="ik-dim">x${r.attempts}</span> ${esc(r.error)}</div>`).join("") || "<i>none</i>";
          return;
        }
        refresh();
      } catch (e) { toast(e.message); }
    }

    document.addEventListener("module-settings-tab", (ev) => { if (ev.detail === spec.tab) render(); });
  }

  const css = `
  .ik-root{display:flex;flex-direction:column;gap:12px;font-size:12px}
  .ik-root h3{font-size:15px;font-weight:700;margin:0}
  .ik-intro{opacity:.8;line-height:1.5}
  .ik-card{border:1px solid rgba(128,128,128,.25);border-radius:8px;padding:10px 12px;display:flex;flex-direction:column;gap:4px}
  .ik-row{display:flex;justify-content:space-between;align-items:center;gap:10px}
  .ik-row>span{opacity:.85}
  .ik-row input:not([type=checkbox]),.ik-row select{width:55%;background:rgba(255,255,255,.06);border:1px solid rgba(128,128,128,.3);border-radius:4px;padding:3px 6px;font-size:12px}
  .ik-help{font-size:11px;opacity:.6;margin:-2px 0 4px}
  .ik-actions{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
  .ik-btn{background:#4f46e5;color:#fff;border:0;border-radius:4px;padding:4px 12px;font-size:12px;cursor:pointer}
  .ik-btn-sm{padding:2px 8px;font-size:11px}.ik-btn-ghost{background:rgba(128,128,128,.2);color:inherit}
  .ik-dim{opacity:.6;font-size:11px}.ik-err{color:#ef4444;font-size:11px}
  .ik-src{border-top:1px solid rgba(128,128,128,.15);padding:8px 0;display:flex;flex-direction:column;gap:3px}
  .ik-bar{height:5px;background:rgba(128,128,128,.2);border-radius:3px;overflow:hidden}.ik-bar>div{height:100%;background:#4f46e5}
  .ik-st{font-size:10px;padding:0 6px;border-radius:3px;background:rgba(128,128,128,.3)}
  .ik-st-running,.ik-st-queued{background:#1d4ed8;color:#fff}.ik-st-done{background:#166534;color:#fff}
  .ik-st-error{background:#b91c1c;color:#fff}.ik-st-canceled{background:#b45309;color:#fff}
  .ik-prompt{border-top:1px dashed rgba(128,128,128,.4);padding-top:6px;margin-top:4px}
  .ik-failures{max-height:200px;overflow:auto;font-size:11px}.ik-fail{padding:2px 0;word-break:break-all}`;
  const st = document.createElement("style"); st.textContent = css; document.head.appendChild(st);

  window.ImportKit = { mount };
})();
