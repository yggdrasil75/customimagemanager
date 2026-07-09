/* IPTC editor.
 *
 * Fetches the merged schema+values structure from /api/iptc/read and renders
 * each record's fields. Enumerated fields become <select> dropdowns showing the
 * human label; scalar fields become text/number inputs; binary fields are shown
 * read-only. Fields the file didn't carry are dimmed and hidden unless "Show
 * empty fields" is on. Tags present on the file but absent from the schema are
 * listed under each record so nothing is silently dropped.
 *
 * Standalone for now (exposes window.iptcEditor); ready to be embedded in the
 * main index. Editing/writeback is intentionally not wired yet — this first pass
 * is import + display. Inputs are left enabled so the write path can hook in
 * later without a markup change.
 */
(function () {
  "use strict";

  const root = () => document.getElementById("iptc-editor");
  const $ = (sel, el) => (el || document).querySelector(sel);

  function setStatus(msg, kind) {
    const s = $("#iptc-status");
    if (!s) return;
    s.textContent = msg || "";
    s.className = "iptc-status" + (kind ? " " + kind : "");
  }

  function esc(v) {
    return String(v == null ? "" : v)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  // ── State ────────────────────────────────────────────────────────────────
  let current = null;        // last-loaded data structure
  let showEmpty = false;
  let showUnmapped = false;

  // ── Render helpers ─────────────────────────────────────────────────────────
  function renderFieldInput(f) {
    const wrap = document.createElement("div");
    wrap.className = "iptc-field-input";

    if (f.dtype === "binary") {
      const span = document.createElement("span");
      span.className = "iptc-binary";
      span.textContent = f.present ? "[binary data present]" : "[none]";
      wrap.appendChild(span);
      return wrap;
    }

    if (f.values) {
      // Enumerated -> dropdown. Options are the raw->label map; we also inject
      // the current raw value if it isn't in the map (unexpected data on file).
      const sel = document.createElement("select");
      sel.disabled = !f.writable;
      const blank = document.createElement("option");
      blank.value = ""; blank.textContent = "— unset —";
      sel.appendChild(blank);
      const rawStr = f.raw == null ? "" : String(f.raw);
      let matched = false;
      Object.keys(f.values).forEach((k) => {
        const opt = document.createElement("option");
        opt.value = k;
        opt.textContent = `${f.values[k]}  (${k})`;
        if (k === rawStr) { opt.selected = true; matched = true; }
        sel.appendChild(opt);
      });
      if (f.present && !matched) {
        const opt = document.createElement("option");
        opt.value = rawStr;
        opt.textContent = `⚠ unknown value (${rawStr})`;
        opt.selected = true;
        sel.appendChild(opt);
      }
      wrap.appendChild(sel);
      return wrap;
    }

    // Scalar text / number input.
    const inp = document.createElement("input");
    const numeric = f.dtype === "int8u" || f.dtype === "int16u" || f.dtype === "int32u";
    inp.type = numeric ? "number" : "text";
    if (f.length) inp.maxLength = f.length;
    inp.value = f.present && f.raw != null ? f.raw : "";
    inp.placeholder = f.present ? "" : "(empty)";
    if (!f.writable) inp.readOnly = true;
    wrap.appendChild(inp);
    return wrap;
  }

  function renderField(f) {
    const tmpl = $("#iptc-field-tmpl");
    const node = tmpl.content.firstElementChild.cloneNode(true);
    node.classList.add(f.present ? "is-present" : "is-empty");
    node.dataset.tagId = f.tag_id;
    node.dataset.name = f.name;

    $(".iptc-field-name", node).textContent = f.name;

    const inputHolder = $(".iptc-field-input", node);
    inputHolder.replaceWith(renderFieldInput(f));

    $(".iptc-field-id", node).textContent = `#${f.tag_id}`;
    $(".iptc-field-type", node).textContent = f.dtype + (f.length ? `[${f.length}]` : "");
    const note = $(".iptc-field-note", node);
    note.textContent = f.note || "";
    return node;
  }

  function renderUnknown(container, unknown) {
    container.innerHTML = "";
    if (!unknown || !unknown.length) return;
    const head = document.createElement("div");
    head.className = "iptc-unknown-head";
    head.textContent = `Unmapped tags on file (${unknown.length})`;
    container.appendChild(head);
    unknown.forEach((u) => {
      const row = document.createElement("div");
      row.className = "iptc-unknown-row";
      row.innerHTML = `<span class="k">${esc(u.name)}</span><span class="v">${esc(fmtRaw(u.raw))}</span>`;
      container.appendChild(row);
    });
  }

  function fmtRaw(v) {
    if (Array.isArray(v)) return v.join(", ");
    if (v && typeof v === "object") return JSON.stringify(v);
    return v;
  }

  function renderRecord(rec) {
    const tmpl = $("#iptc-record-tmpl");
    const node = tmpl.content.firstElementChild.cloneNode(true);
    if (!rec.mapped) node.classList.add("is-unmapped");

    $(".iptc-record-num", node).textContent = rec.number != null ? `[${rec.number}]` : "[?]";
    $(".iptc-record-title", node).textContent = rec.title;
    $(".iptc-record-desc", node).textContent = rec.description || "";

    const badge = $(".iptc-record-badge", node);
    badge.textContent = rec.mapped ? "mapped" : "unmapped";
    badge.classList.add(rec.mapped ? "mapped" : "unmapped");

    // Collapse/expand on header click.
    $(".iptc-record-head", node).addEventListener("click", () => {
      node.classList.toggle("collapsed");
    });

    const fieldsHolder = $(".iptc-record-fields", node);
    let shown = 0;
    (rec.fields || []).forEach((f) => {
      if (!f.present && !showEmpty) return;
      fieldsHolder.appendChild(renderField(f));
      shown++;
    });
    if (!shown && rec.mapped) {
      const div = document.createElement("div");
      div.className = "iptc-empty";
      div.textContent = showEmpty ? "No fields." : "No populated fields (toggle “Show empty fields”).";
      fieldsHolder.appendChild(div);
    }

    renderUnknown($(".iptc-record-unknown", node), rec.unknown);
    return node;
  }

  function render() {
    const recEl = $("#iptc-records");
    if (!current) { return; }
    recEl.innerHTML = "";

    const recs = current.records.filter((r) => showUnmapped || r.mapped ||
      (r.unknown && r.unknown.length));
    if (!recs.length) {
      recEl.innerHTML = `<div class="iptc-empty">No records to show. Toggle “Show unmapped records”.</div>`;
    } else {
      recs.forEach((r) => recEl.appendChild(renderRecord(r)));
    }

    // Summary line.
    const present = current.records.reduce(
      (n, r) => n + (r.fields || []).filter((f) => f.present).length, 0);
    const unknown = current.records.reduce((n, r) => n + (r.unknown ? r.unknown.length : 0), 0);
    $("#iptc-summary").innerHTML =
      `<span><b>${present}</b> populated field${present === 1 ? "" : "s"}</span>` +
      `<span><b>${unknown}</b> unmapped tag${unknown === 1 ? "" : "s"}</span>`;
    const src = $("#iptc-source");
    if (src) src.textContent = current.source ? `← ${current.source}` : "(no IPTC found)";
  }

  // ── Load ───────────────────────────────────────────────────────────────────
  async function load(filename) {
    if (!filename) { setStatus("no file", "err"); return; }
    root().dataset.filename = filename;
    setStatus("loading…");
    try {
      const r = await fetch("/api/iptc/read", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename }),
      });
      const d = await r.json();
      if (!d.success) throw new Error(d.error || "read failed");
      current = d.data;
      render();
      setStatus("loaded", "ok");
    } catch (e) {
      current = null;
      $("#iptc-records").innerHTML =
        `<div class="iptc-empty">Failed to read IPTC: ${esc(e.message)}</div>`;
      $("#iptc-summary").innerHTML = "";
      setStatus("error", "err");
    }
  }

  // ── Wiring ──────────────────────────────────────────────────────────────────
  function init() {
    if (!root()) return;
    const se = $("#iptc-show-empty");
    const su = $("#iptc-show-unmapped");
    if (se) se.addEventListener("change", (e) => { showEmpty = e.target.checked; render(); });
    if (su) su.addEventListener("change", (e) => { showUnmapped = e.target.checked; render(); });
    const rl = $("#iptc-reload");
    if (rl) rl.addEventListener("click", () => {
      const fn = root().dataset.filename;
      if (fn) load(fn);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  // Public API for the host page / future index embedding.
  window.iptcEditor = { load, refresh: render };
})();