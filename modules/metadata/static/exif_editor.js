/* EXIF editor.
 *
 * Sibling of iptc_editor.js. Fetches the merged schema+values structure from
 * /api/exif/read and renders each group's fields. Enumerated fields become
 * <select> dropdowns showing the human label; scalar numeric fields become
 * number inputs; short strings become text inputs; multiline strings become
 * textareas; undef/binary fields render read-only. Fields the file didn't carry
 * are dimmed and hidden unless "Show empty fields" is on. Tags present on the
 * file but absent from the schema are listed under each group so nothing is
 * silently dropped.
 *
 * Unlike the IPTC editor's first pass, editing IS wired: changed fields are
 * tracked and POSTed to /api/exif/write as a {tag_name: value} patch. Read-only
 * fields are shown disabled and never included in the patch. Exposes
 * window.exifEditor.
 */
(function () {
  "use strict";

  const root = () => document.getElementById("exif-editor");
  const $ = (sel, el) => (el || document).querySelector(sel);

  function setStatus(msg, kind) {
    const s = $("#exif-status");
    if (!s) return;
    s.textContent = msg || "";
    s.className = "exif-status" + (kind ? " " + kind : "");
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
  const dirty = new Map();   // tag_name -> new value (writable, changed fields)

  const NUMERIC = new Set(["int8u", "int16u", "int32u"]);

  function markDirty(tagName, value, original) {
    const same = String(value) === String(original == null ? "" : original);
    if (same) dirty.delete(tagName);
    else dirty.set(tagName, value);
    const btn = $("#exif-save");
    if (btn) btn.disabled = dirty.size === 0;
    const s = $("#exif-status");
    if (dirty.size) setStatus(`${dirty.size} unsaved change${dirty.size === 1 ? "" : "s"}`);
    else if (s && s.textContent.indexOf("unsaved") !== -1) setStatus("");
  }

  // ── Render helpers ─────────────────────────────────────────────────────────
  function renderFieldInput(f) {
    const wrap = document.createElement("div");
    wrap.className = "exif-field-input";
    const original = f.present && f.raw != null ? f.raw : "";
    // Generated fields (e.g. ImageHistory, CompressedBitsPerPixel, SubjectDistance)
    // are written by the app, not hand-edited: show their value but don't let the
    // user change it here. The backend still accepts programmatic writes.
    const editable = f.writable && !f.generated;

    if (f.dtype === "binary" || f.dtype === "undef") {
      const span = document.createElement("span");
      span.className = "exif-binary";
      span.textContent = f.present
        ? (f.dtype === "undef" ? String(f.raw) : "[binary data present]")
        : "[none]";
      wrap.appendChild(span);
      return wrap;
    }

    if (f.values) {
      // Enumerated -> dropdown. Options are the raw->label map; we also inject
      // the current raw value if it isn't in the map (unexpected data on file).
      const sel = document.createElement("select");
      sel.disabled = !editable;
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
      if (editable) {
        sel.addEventListener("change", () => markDirty(f.name, sel.value, original));
      }
      wrap.appendChild(sel);
      return wrap;
    }

    // Multiline string -> textarea.
    if (f.multiline) {
      const ta = document.createElement("textarea");
      ta.rows = 2;
      ta.value = original;
      ta.placeholder = f.present ? "" : "(empty)";
      if (!editable) ta.readOnly = true;
      else ta.addEventListener("input", () => markDirty(f.name, ta.value, original));
      wrap.appendChild(ta);
      return wrap;
    }

    // Scalar number / short-string input.
    const inp = document.createElement("input");
    const numeric = NUMERIC.has(f.dtype);
    inp.type = numeric ? "number" : "text";
    if (f.length) inp.maxLength = f.length;
    inp.value = original;
    inp.placeholder = f.present ? "" : "(empty)";
    if (!editable) inp.readOnly = true;
    else inp.addEventListener("input", () => markDirty(f.name, inp.value, original));
    wrap.appendChild(inp);
    return wrap;
  }

  function renderField(f) {
    const tmpl = $("#exif-field-tmpl");
    const node = tmpl.content.firstElementChild.cloneNode(true);
    node.classList.add(f.present ? "is-present" : "is-empty");
    if (!f.writable) node.classList.add("is-readonly");
    node.dataset.tagId = f.tag_id;
    node.dataset.name = f.name;

    $(".exif-field-name", node).textContent = f.name;

    const inputHolder = $(".exif-field-input", node);
    inputHolder.replaceWith(renderFieldInput(f));

    $(".exif-field-id", node).textContent = f.tag_hex || `#${f.tag_id}`;
    const cnt = f.count === 0 ? "[n]" : (f.count ? `[${f.count}]` : (f.length ? `[${f.length}]` : ""));
    $(".exif-field-type", node).textContent = f.dtype + cnt;
    const roEl = $(".exif-field-ro", node);
    if (f.generated) { roEl.textContent = "generated"; roEl.classList.add("is-generated"); }
    else roEl.textContent = f.writable ? "" : "read-only";
    const note = $(".exif-field-note", node);
    note.textContent = f.note || "";
    return node;
  }

  function fmtRaw(v) {
    if (Array.isArray(v)) return v.join(", ");
    if (v && typeof v === "object") return JSON.stringify(v);
    return v;
  }

  function renderUnknown(container, unknown) {
    container.innerHTML = "";
    if (!unknown || !unknown.length) return;
    const head = document.createElement("div");
    head.className = "exif-unknown-head";
    head.textContent = `Unmapped tags on file (${unknown.length})`;
    container.appendChild(head);
    unknown.forEach((u) => {
      const row = document.createElement("div");
      row.className = "exif-unknown-row";
      row.innerHTML = `<span class="k">${esc(u.name)}</span><span class="v">${esc(fmtRaw(u.raw))}</span>`;
      container.appendChild(row);
    });
  }

  function renderGroup(grp) {
    const tmpl = $("#exif-group-tmpl");
    const node = tmpl.content.firstElementChild.cloneNode(true);
    if (!grp.mapped) node.classList.add("is-unmapped");

    $(".exif-group-ifd", node).textContent = grp.ifd ? `[${grp.ifd}]` : "[?]";
    $(".exif-group-title", node).textContent = grp.title;
    $(".exif-group-desc", node).textContent = grp.description || "";

    const badge = $(".exif-group-badge", node);
    badge.textContent = grp.mapped ? "mapped" : "unmapped";
    badge.classList.add(grp.mapped ? "mapped" : "unmapped");

    // Collapse/expand on header click.
    $(".exif-group-head", node).addEventListener("click", () => {
      node.classList.toggle("collapsed");
    });

    const fieldsHolder = $(".exif-group-fields", node);
    let shown = 0;
    (grp.fields || []).forEach((f) => {
      if (!f.present && !showEmpty) return;
      fieldsHolder.appendChild(renderField(f));
      shown++;
    });
    if (!shown && grp.mapped) {
      const div = document.createElement("div");
      div.className = "exif-empty";
      div.textContent = showEmpty ? "No fields." : "No populated fields (toggle “Show empty fields”).";
      fieldsHolder.appendChild(div);
    }

    renderUnknown($(".exif-group-unknown", node), grp.unknown);
    return node;
  }

  function render() {
    const grpEl = $("#exif-groups");
    if (!current) { return; }
    grpEl.innerHTML = "";

    const grps = current.groups.filter((g) => showUnmapped || g.mapped ||
      (g.unknown && g.unknown.length));
    if (!grps.length) {
      grpEl.innerHTML = `<div class="exif-empty">No groups to show. Toggle “Show unmapped groups”.</div>`;
    } else {
      grps.forEach((g) => grpEl.appendChild(renderGroup(g)));
    }

    // Summary line.
    const present = current.groups.reduce(
      (n, g) => n + (g.fields || []).filter((f) => f.present).length, 0);
    const unknown = current.groups.reduce((n, g) => n + (g.unknown ? g.unknown.length : 0), 0);
    $("#exif-summary").innerHTML =
      `<span><b>${present}</b> populated field${present === 1 ? "" : "s"}</span>` +
      `<span><b>${unknown}</b> unmapped tag${unknown === 1 ? "" : "s"}</span>`;
    const src = $("#exif-source");
    if (src) src.textContent = current.source ? `← ${current.source}` : "(no EXIF found)";
  }

  // ── Load ───────────────────────────────────────────────────────────────────
  async function load(filename) {
    if (!filename) { setStatus("no file", "err"); return; }
    root().dataset.filename = filename;
    dirty.clear();
    const btn = $("#exif-save");
    if (btn) btn.disabled = true;
    setStatus("loading…");
    try {
      const r = await fetch("/api/exif/read", {
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
      $("#exif-groups").innerHTML =
        `<div class="exif-empty">Failed to read EXIF: ${esc(e.message)}</div>`;
      $("#exif-summary").innerHTML = "";
      setStatus("error", "err");
    }
  }

  // ── Save ───────────────────────────────────────────────────────────────────
  async function save() {
    const filename = root().dataset.filename;
    if (!filename || dirty.size === 0) return;
    const patch = {};
    dirty.forEach((v, k) => { patch[k] = v; });
    setStatus("saving…");
    const btn = $("#exif-save");
    if (btn) btn.disabled = true;
    try {
      const r = await fetch("/api/exif/write", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename, patch }),
      });
      const d = await r.json();
      if (!d.success) throw new Error(d.error || "write failed");
      const res = d.result || {};
      const nw = (res.written || []).length;
      const nd = (res.deleted || []).length;
      const nr = (res.rejected || []).length;
      const ns = (res.skipped || []).length;
      let msg = `saved ${nw} written, ${nd} deleted`;
      if (nr) msg += `, ${nr} rejected`;
      if (ns) msg += `, ${ns} skipped`;
      setStatus(msg, nr ? "err" : "ok");
      dirty.clear();
      await load(filename);   // reload to reflect what actually stuck
    } catch (e) {
      setStatus("save error: " + e.message, "err");
      if (btn) btn.disabled = false;
    }
  }

  // ── Wiring ──────────────────────────────────────────────────────────────────
  function init() {
    if (!root()) return;
    const se = $("#exif-show-empty");
    const su = $("#exif-show-unmapped");
    if (se) se.addEventListener("change", (e) => { showEmpty = e.target.checked; render(); });
    if (su) su.addEventListener("change", (e) => { showUnmapped = e.target.checked; render(); });
    const rl = $("#exif-reload");
    if (rl) rl.addEventListener("click", () => {
      const fn = root().dataset.filename;
      if (fn) load(fn);
    });
    const sv = $("#exif-save");
    if (sv) sv.addEventListener("click", save);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  // Public API for the host page / future index embedding.
  window.exifEditor = { load, save, refresh: render };
})();