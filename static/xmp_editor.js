/* XMP editor (read-only for the acdsee namespace).
 *
 * Fetches the merged schema+values structure from /api/xmp/read and renders each
 * namespace's fields. Because the acdsee set is retrieval-only in this project,
 * every input is rendered read-only — this is an inspector, not a writer. Lang-alt
 * blocks (DPP/RPP) and bag/seq lists (Keywords/Snapshots) get formatted display.
 * Fields the file didn't carry are dimmed and hidden unless "Show empty fields" is
 * on. Fields that fold into our own description/tags/rating are badged so it's
 * clear where the value ends up. Tags present on the file but absent from the
 * schema are listed under each namespace so nothing is silently dropped.
 *
 * Mirrors static/iptc_editor.js in structure; standalone for now (exposes
 * window.xmpEditor), ready to be embedded in the main index.
 */
(function () {
  "use strict";

  const root = () => document.getElementById("xmp-editor");
  const $ = (sel, el) => (el || document).querySelector(sel);

  function setStatus(msg, kind) {
    const s = $("#xmp-status");
    if (!s) return;
    s.textContent = msg || "";
    s.className = "xmp-status" + (kind ? " " + kind : "");
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

  // ── Value formatting ───────────────────────────────────────────────────────
  // XMP lang-alt comes back from pyexiv2 as either a plain string or a dict of
  // {lang: text} (e.g. {"x-default": "..."}); lists come back as arrays.
  function fmtRaw(v) {
    if (v == null) return "";
    if (Array.isArray(v)) return v.join(", ");
    if (typeof v === "object") {
      // lang-alt or struct: show "lang: text" lines, prefer x-default first.
      const keys = Object.keys(v);
      keys.sort((a, b) => (a === "x-default" ? -1 : b === "x-default" ? 1 : 0));
      return keys.map((k) => `${k}: ${v[k]}`).join("\n");
    }
    return String(v);
  }

  // ── Render helpers ─────────────────────────────────────────────────────────
  function renderFieldInput(f) {
    const wrap = document.createElement("div");
    wrap.className = "xmp-field-input";

    const shown = f.present ? fmtRaw(f.display != null ? f.display : f.raw) : "";

    // lang-alt and long list values render as a read-only textarea so the XML
    // raw-processing blobs (DPP/RPP) and multi-line lang-alt are legible.
    const multiline =
      f.dtype === "lang-alt" ||
      (f.present && typeof shown === "string" && shown.indexOf("\n") !== -1) ||
      (f.is_list && shown.length > 60);

    if (multiline) {
      const ta = document.createElement("textarea");
      ta.readOnly = true;                 // acdsee is retrieval-only
      ta.rows = Math.min(8, Math.max(2, String(shown).split("\n").length));
      ta.value = shown;
      ta.placeholder = f.present ? "" : "(empty)";
      wrap.appendChild(ta);
      return wrap;
    }

    const inp = document.createElement("input");
    inp.type = "text";
    inp.readOnly = true;                  // acdsee is retrieval-only
    inp.value = shown;
    inp.placeholder = f.present ? "" : "(empty)";
    wrap.appendChild(inp);
    return wrap;
  }

  function renderField(f) {
    const tmpl = $("#xmp-field-tmpl");
    const node = tmpl.content.firstElementChild.cloneNode(true);
    node.classList.add(f.present ? "is-present" : "is-empty");
    node.dataset.name = f.name;

    $(".xmp-field-name", node).textContent = f.name;

    const inputHolder = $(".xmp-field-input", node);
    inputHolder.replaceWith(renderFieldInput(f));

    const type = f.dtype + (f.is_list ? "[]" : "") + (f.writable ? "" : " · read-only");
    $(".xmp-field-type", node).textContent = type;

    const feeds = $(".xmp-field-feeds", node);
    if (f.feeds) {
      feeds.textContent = `→ ${f.feeds}`;
      feeds.classList.add("xmp-feeds-" + f.feeds);
      feeds.title = `Folded into our ${f.feeds} field on ingest`;
    } else {
      feeds.textContent = "";
    }

    $(".xmp-field-note", node).textContent = f.note || "";
    return node;
  }

  function renderUnknown(container, unknown) {
    container.innerHTML = "";
    if (!unknown || !unknown.length) return;
    const head = document.createElement("div");
    head.className = "xmp-unknown-head";
    head.textContent = `Unmapped tags on file (${unknown.length})`;
    container.appendChild(head);
    unknown.forEach((u) => {
      const row = document.createElement("div");
      row.className = "xmp-unknown-row";
      row.innerHTML =
        `<span class="k">${esc(u.name)}</span>` +
        `<span class="v">${esc(fmtRaw(u.raw))}</span>`;
      container.appendChild(row);
    });
  }

  function renderNamespace(ns) {
    const tmpl = $("#xmp-namespace-tmpl");
    const node = tmpl.content.firstElementChild.cloneNode(true);
    if (!ns.mapped) node.classList.add("is-unmapped");

    $(".xmp-namespace-ns", node).textContent = `[${ns.ns}]`;
    $(".xmp-namespace-title", node).textContent = ns.title;
    $(".xmp-namespace-desc", node).textContent = ns.description || "";

    const badge = $(".xmp-namespace-badge", node);
    badge.textContent = ns.mapped ? "mapped" : "unmapped";
    badge.classList.add(ns.mapped ? "mapped" : "unmapped");

    // Collapse/expand on header click.
    $(".xmp-namespace-head", node).addEventListener("click", () => {
      node.classList.toggle("collapsed");
    });

    const fieldsHolder = $(".xmp-namespace-fields", node);
    let shown = 0;
    (ns.fields || []).forEach((f) => {
      if (!f.present && !showEmpty) return;
      fieldsHolder.appendChild(renderField(f));
      shown++;
    });
    if (!shown && ns.mapped) {
      const div = document.createElement("div");
      div.className = "xmp-empty";
      div.textContent = showEmpty
        ? "No fields."
        : "No populated fields (toggle “Show empty fields”).";
      fieldsHolder.appendChild(div);
    }

    renderUnknown($(".xmp-namespace-unknown", node), ns.unknown);
    return node;
  }

  function render() {
    const nsEl = $("#xmp-namespaces");
    if (!current) { return; }
    nsEl.innerHTML = "";

    const nss = current.namespaces.filter((n) => showUnmapped || n.mapped ||
      (n.unknown && n.unknown.length));
    if (!nss.length) {
      nsEl.innerHTML =
        `<div class="xmp-empty">No namespaces to show. Toggle “Show unmapped namespaces”.</div>`;
    } else {
      nss.forEach((n) => nsEl.appendChild(renderNamespace(n)));
    }

    // Summary line.
    const present = current.namespaces.reduce(
      (n, ns) => n + (ns.fields || []).filter((f) => f.present).length, 0);
    const unknown = current.namespaces.reduce(
      (n, ns) => n + (ns.unknown ? ns.unknown.length : 0), 0);
    $("#xmp-summary").innerHTML =
      `<span><b>${present}</b> populated field${present === 1 ? "" : "s"}</span>` +
      `<span><b>${unknown}</b> unmapped tag${unknown === 1 ? "" : "s"}</span>`;
    const src = $("#xmp-source");
    if (src) src.textContent = current.source ? `← ${current.source}` : "(no XMP found)";
  }

  // ── Load ───────────────────────────────────────────────────────────────────
  async function load(filename) {
    if (!filename) { setStatus("no file", "err"); return; }
    root().dataset.filename = filename;
    setStatus("loading…");
    try {
      const r = await fetch("/api/xmp/read", {
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
      $("#xmp-namespaces").innerHTML =
        `<div class="xmp-empty">Failed to read XMP: ${esc(e.message)}</div>`;
      $("#xmp-summary").innerHTML = "";
      setStatus("error", "err");
    }
  }

  // ── Wiring ──────────────────────────────────────────────────────────────────
  function init() {
    if (!root()) return;
    const se = $("#xmp-show-empty");
    const su = $("#xmp-show-unmapped");
    if (se) se.addEventListener("change", (e) => { showEmpty = e.target.checked; render(); });
    if (su) su.addEventListener("change", (e) => { showUnmapped = e.target.checked; render(); });
    const rl = $("#xmp-reload");
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
  window.xmpEditor = { load, refresh: render };
})();