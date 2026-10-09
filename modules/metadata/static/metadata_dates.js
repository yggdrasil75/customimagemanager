/** @file metadata_dates.js
 *  @brief The "Date taken" row under the description in the editor pane: shows the
 *  open file's taken date, time and offset (GET /api/metadata/date) and edits them
 *  (POST /api/metadata/date): date, time, time zone, how a zone change is applied
 *  (keep the local clock time, or keep the instant and move the clock) and which
 *  EXIF / XMP fields are written. Seeing it needs read on meta.exif, editing write.
 */
(function () {
  "use strict";

  const FIELD_LABELS = {
    "DateTimeOriginal": "EXIF DateTimeOriginal (+ OffsetTimeOriginal)",
    "CreateDate": "EXIF DateTimeDigitized (+ OffsetTimeDigitized)",
    "photoshop:DateCreated": "XMP photoshop:DateCreated",
    "exif:DateTimeOriginal": "XMP exif:DateTimeOriginal",
    "xmp:CreateDate": "XMP xmp:CreateDate",
  };
  const S = { file: "", data: null, gen: 0 };
  const esc = s => (typeof _esc === "function") ? _esc(s) : String(s ?? "");
  const toast = m => (typeof showToast === "function") ? showToast(m) : console.log(m);

  /** @brief "+HH:MM" for minutes east of UTC. */
  function fmtOffset(min) {
    const a = Math.abs(min);
    return (min < 0 ? "-" : "+") + String(Math.floor(a / 60)).padStart(2, "0") + ":" + String(a % 60).padStart(2, "0");
  }

  /** @brief The offset choices, -12:00 .. +14:00 in quarter hours. */
  function offsetOptions() {
    let html = '<option value="">No time zone</option>';
    for (let m = -12 * 60; m <= 14 * 60; m += 15) {
      const v = fmtOffset(m);
      html += `<option value="${v}">UTC${v}</option>`;
    }
    return html;
  }

  /** @brief Build the row once, right after the description block. */
  function ensureRow() {
    let row = document.getElementById("meta_date_row");
    if (row) return row;
    const desc = document.getElementById("meta_desc");
    const block = desc && desc.parentElement;
    if (!block || !block.parentElement) return null;
    row = document.createElement("div");
    row.id = "meta_date_row";
    row.setAttribute("data-feature", "meta.exif");
    row.innerHTML = `
      <div class="flex justify-between items-center mb-1">
        <label class="text-[10px] font-bold text-gray-400 uppercase tracking-wider">Date taken</label>
        <span data-write-gate="meta.exif">${cimButton({ label: "Edit", variant: "neutral", size: "xs",
          id: "meta_date_edit", title: "Change the date, time or time zone", onclick: "metaDateEdit()" })}</span>
      </div>
      <div id="meta_date_value" class="text-xs text-gray-300"></div>
      <div id="meta_date_form" class="hidden mt-1 p-2 bg-gray-900 border border-gray-600 rounded flex flex-col gap-1.5 text-xs"
           data-write-gate="meta.exif">
        <div class="flex gap-1">
          <input id="meta_date_d" type="date" class="flex-1 min-w-0 p-1 bg-gray-700 rounded border border-gray-600 text-white">
          <input id="meta_date_t" type="time" step="1" class="w-28 p-1 bg-gray-700 rounded border border-gray-600 text-white">
        </div>
        <select id="meta_date_tz" class="p-1 bg-gray-700 rounded border border-gray-600 text-white"
                title="Time zone (offset from UTC)">${offsetOptions()}</select>
        <div class="flex flex-col gap-0.5 text-gray-300" title="Only matters when the time zone changes">
          <label class="flex items-center gap-1"><input type="radio" name="meta_date_mode" value="keep_local" checked>
            Keep the local time, change the zone</label>
          <label class="flex items-center gap-1"><input type="radio" name="meta_date_mode" value="keep_instant" id="meta_date_instant">
            Keep the instant, move the clock to the new zone</label>
        </div>
        <details class="text-gray-400">
          <summary class="cursor-pointer select-none" data-gate-keep>Fields to write</summary>
          <div id="meta_date_fields" class="flex flex-col gap-0.5 mt-1"></div>
        </details>
        <div class="flex gap-1 justify-end">
          ${cimButton({ label: "Cancel", variant: "neutral", size: "xs", onclick: "metaDateCancel()" })}
          ${cimButton({ label: "Save", variant: "primary", size: "xs", id: "meta_date_save", onclick: "metaDateSave()" })}
        </div>
      </div>`;
    block.parentElement.insertBefore(row, block.nextSibling);
    if (window.CIMFeatures) window.CIMFeatures.apply(row);
    return row;
  }

  /** @brief Show the loaded date in the value line. */
  function renderValue() {
    const el = document.getElementById("meta_date_value");
    if (!el) return;
    const d = S.data;
    if (!S.file || !d) { el.textContent = ""; return; }
    if (d.datetime) {
      const when = d.datetime.replace("T", " ");
      el.innerHTML = `${esc(when)} <span class="text-gray-400">${esc(d.offset || "no time zone")}</span>` +
        (d.source ? ` <span class="text-gray-500" title="Read from">(${esc(d.source)})</span>` : "");
      return;
    }
    const b = d.buckets || {};
    const day = b.d_original || b.d_capture || b.d_actual || b.d_digitized;
    el.innerHTML = day
      ? `${esc(day)} <span class="text-gray-500">(date only, no time stored)</span>`
      : '<span class="text-gray-500 italic">Unknown</span>';
  }

  /** @brief Load the open file's date. */
  async function load(filename) {
    S.file = filename || "";
    S.data = null;
    const gen = ++S.gen;
    if (!ensureRow()) return;
    metaDateCancel();
    renderValue();
    if (!S.file) return;
    try {
      const r = await fetch("/api/metadata/date?filename=" + encodeURIComponent(S.file));
      const d = await r.json();
      if (gen !== S.gen) return;
      S.data = d && d.success ? d : null;
    } catch (e) {
      if (gen !== S.gen) return;
      S.data = null;
    }
    renderValue();
  }

  /** @brief Open the edit form, filled from the loaded date. */
  window.metaDateEdit = function () {
    if (!S.file || !ensureRow()) return;
    const d = S.data || {};
    const b = d.buckets || {};
    const iso = d.datetime || ((b.d_original || b.d_capture || b.d_actual || b.d_digitized || "") + "T12:00:00");
    const [day, time] = iso.length > 11 ? iso.split("T") : ["", ""];
    document.getElementById("meta_date_d").value = day || "";
    document.getElementById("meta_date_t").value = (time || "").slice(0, 8);
    document.getElementById("meta_date_tz").value = d.offset || "";
    const inst = document.getElementById("meta_date_instant");
    inst.disabled = !d.offset;
    inst.title = d.offset ? "" : "The file has no time zone yet";
    document.querySelector('input[name="meta_date_mode"][value="keep_local"]').checked = true;
    const present = new Set(d.fields_present || []);
    const defaults = new Set(d.default_fields || []);
    const all = d.fields || Object.keys(FIELD_LABELS);
    document.getElementById("meta_date_fields").innerHTML = all.map(f =>
      `<label class="flex items-center gap-1"><input type="checkbox" value="${esc(f)}"
         ${defaults.has(f) || present.has(f) ? "checked" : ""}> ${esc(FIELD_LABELS[f] || f)}</label>`).join("");
    document.getElementById("meta_date_form").classList.remove("hidden");
    if (window.CIMFeatures) window.CIMFeatures.apply(document.getElementById("meta_date_row"));
  };

  /** @brief Close the edit form without saving. */
  window.metaDateCancel = function () {
    const f = document.getElementById("meta_date_form");
    if (f) f.classList.add("hidden");
  };

  /** @brief Write the edited date (POST /api/metadata/date) and reload the row. */
  window.metaDateSave = async function () {
    if (!S.file) return;
    const day = document.getElementById("meta_date_d").value;
    let time = document.getElementById("meta_date_t").value || "00:00:00";
    if (!day) { toast("Pick a date first."); return; }
    if (time.length === 5) time += ":00";
    const mode = (document.querySelector('input[name="meta_date_mode"]:checked') || {}).value || "keep_local";
    const fields = [...document.querySelectorAll("#meta_date_fields input:checked")].map(i => i.value);
    if (!fields.length) { toast("Choose at least one field to write."); return; }
    const body = { filename: S.file, datetime: `${day}T${time}`,
                   offset: document.getElementById("meta_date_tz").value || null,
                   fields, tz_mode: mode };
    if (mode === "keep_instant" && S.data && S.data.offset) body.from_offset = S.data.offset;
    const btn = document.getElementById("meta_date_save");
    if (btn) btn.disabled = true;
    try {
      const r = await fetch("/api/metadata/date", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const d = await r.json().catch(() => ({ success: false, error: "HTTP " + r.status }));
      if (!d.success) { toast("Date not saved: " + (d.error || "error")); return; }
      toast(`Date set to ${d.datetime.replace("T", " ")}${d.offset ? " " + d.offset : ""}`);
      await load(S.file);
    } finally {
      if (btn) btn.disabled = false;
    }
  };

  function init() {
    ensureRow();
    if (window.registerFileMetaHook) registerFileMetaHook((meta, filename) => load(filename || window.currentFile));
  }
  if (document.readyState === "loading") window.addEventListener("DOMContentLoaded", init);
  else init();
})();
