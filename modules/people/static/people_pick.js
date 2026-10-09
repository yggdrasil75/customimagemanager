/* people_pick.js - shared people helpers (window.CIMPeople) for the People tab, the
 * viewer's region list and the layouts' "people in this photo" chips:
 *
 *   CIMPeople.facesIn(filename)        -> Promise<[{id,cx,cy,w,h,cluster_id,name,uuid,favorite,hidden}]>
 *   CIMPeople.matchFace(faces, region) -> the cached face for a region (box, else name)
 *   CIMPeople.cropStyle(box, pad)      -> inline background-size/-position cropping a box out of a thumb
 *   CIMPeople.pick({title, allowRemove}) -> Promise<{person_id|cluster_id|name} | {remove:true} | null>
 *   CIMPeople.changeFace(target, opts) -> pick + POST /api/faces/assign | /api/faces/unassign
 *   CIMPeople.changeRegion(i)          -> the same for currentRegions[i] of the open picture
 *   CIMPeople.search(clusterId)        -> the gallery filtered to person:<id>
 *   CIMPeople.setFavorite(who, on) / CIMPeople.setHidden(who, hidden)
 *
 * A change fires `cim:people-changed` on window with {filename, ...result}.
 * The picker lists favourites first, then the people this browser picked recently
 * (a per-viewer convenience kept in localStorage), then everyone else by name.
 */
(function () {
  "use strict";
  const RECENT_KEY = "cim.people.recent";
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const post = (url, body) => fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                                           body: JSON.stringify(body || {}) }).then(r => r.json());
  const clamp01 = v => v < 0 ? 0 : v > 1 ? 1 : v;
  const faceCache = new Map();          // filename -> Promise<faces>

  /** @brief Background crop of a normalised box out of the full thumbnail (see faceChip). */
  function cropStyle(f, pad = 1.6) {
    const bw = Math.max(+f.w || 0, 0.01) * pad, bh = Math.max(+f.h || 0, 0.01) * pad;
    const px = bw >= 1 ? 50 : clamp01((f.cx - bw / 2) / (1 - bw)) * 100;
    const py = bh >= 1 ? 50 : clamp01((f.cy - bh / 2) / (1 - bh)) * 100;
    return `background-size:${100 / bw}% ${100 / bh}%;background-position:${px}% ${py}%`;
  }

  /** @brief The cached faces of one picture (one request per file until something changes). */
  function facesIn(filename) {
    if (!filename) return Promise.resolve([]);
    if (!faceCache.has(filename)) {
      faceCache.set(filename, fetch("/api/faces/in_file?filename=" + encodeURIComponent(filename))
        .then(r => r.json()).then(d => (d && d.success && d.faces) || [])
        .catch(() => { faceCache.delete(filename); return []; }));
    }
    return faceCache.get(filename);
  }
  const isFace = r => !!r && [r.class_name, r.region_type].some(k => String(k || "").toLowerCase() === "face");

  /** @brief The cached face for a region: same box, else (a named body box) the same name. */
  function matchFace(faces, r) {
    if (!r) return null;
    const box = (faces || []).find(f => Math.abs(f.cx - r.cx) < 2e-3 && Math.abs(f.cy - r.cy) < 2e-3);
    if (box) return box;
    const name = String(r.region_name || "").trim().toLowerCase();
    return name ? (faces || []).find(f => f.cluster_id >= 0 && String(f.name).toLowerCase() === name) || null : null;
  }

  function readRecent() {
    try { const v = JSON.parse(localStorage.getItem(RECENT_KEY) || "[]"); return Array.isArray(v) ? v : []; }
    catch (e) { return []; }
  }
  function noteRecent(key) {
    if (!key) return;
    try { localStorage.setItem(RECENT_KEY, JSON.stringify([key, ...readRecent().filter(k => k !== key)].slice(0, 12))); }
    catch (e) { /* storage unavailable: no recents */ }
  }
  const keyOf = p => p.uuid ? "u:" + p.uuid : (p.cluster_id != null ? "c:" + p.cluster_id : "");

  /** @brief Favourites first, then recently picked, then by name (the directory's own order). */
  function orderPeople(people) {
    const recent = readRecent();
    const rank = p => p.favorite ? -1000 : (recent.indexOf(keyOf(p)) >= 0 ? recent.indexOf(keyOf(p)) - 100 : 0);
    return people.map((p, i) => [p, i]).sort((a, b) => rank(a[0]) - rank(b[0]) || a[1] - b[1]).map(x => x[0]);
  }

  /** @brief A modal person picker: type to search the directory, Enter takes the first row. */
  function pick(opts) {
    opts = opts || {};
    document.getElementById("cim_people_picker")?.remove();
    return new Promise(resolve => {
      const host = document.createElement("div");
      host.id = "cim_people_picker";
      host.className = "fixed inset-0 flex items-center justify-center";
      host.style.cssText = "z-index:300;background:var(--cim-scrim, rgba(0,0,0,.6))";
      host.innerHTML = `
        <div class="bg-gray-800 border border-gray-600 rounded shadow-xl p-3 flex flex-col gap-2" style="width:20rem;max-width:92vw">
          <div class="text-sm font-bold text-gray-100">${esc(opts.title || "Change person")}</div>
          <input id="cim_pp_q" autocomplete="off" placeholder="Search by name, or type a new one"
                 class="p-1.5 bg-gray-700 rounded border border-gray-600 text-sm text-white">
          <div id="cim_pp_list" class="overflow-y-auto flex flex-col" style="max-height:18rem">
            <div class="text-xs text-gray-500 p-1">Loading...</div></div>
          <div class="flex items-center gap-2">
            ${opts.allowRemove ? cimButtonHtml({ id: "cim_pp_remove", variant: "danger", label: "Remove from person",
                                                 title: "This face is nobody you know: unname it (unknown face)" }) : ""}
            <span class="flex-1"></span>
            ${cimButtonHtml({ id: "cim_pp_cancel", variant: "neutral", label: "Cancel" })}
          </div>
        </div>`;
      document.body.appendChild(host);
      let people = [], shown = [];
      const q = host.querySelector("#cim_pp_q"), list = host.querySelector("#cim_pp_list");
      const done = v => { host.remove(); document.removeEventListener("keydown", onKey, true); resolve(v); };
      const choose = p => {
        if (p.newName) { done({ name: p.newName }); return; }
        noteRecent(keyOf(p));
        done(p.uuid ? { person_id: p.uuid, name: p.name } : { cluster_id: p.cluster_id, name: p.name });
      };
      function render() {
        const t = q.value.trim().toLowerCase();
        shown = people.filter(p => !t || p.name.toLowerCase().includes(t));
        if (t && !people.some(p => p.name.toLowerCase() === t)) shown.push({ newName: q.value.trim() });
        list.innerHTML = shown.length ? shown.map((p, i) => p.newName
          ? `<button type="button" data-i="${i}" class="text-left text-sm px-2 py-1 rounded hover:bg-gray-700 text-blue-300">+ New person "${esc(p.newName)}"</button>`
          : `<button type="button" data-i="${i}" class="text-left text-sm px-2 py-1 rounded hover:bg-gray-700 text-gray-100 flex items-center gap-1">
               <span class="${p.favorite ? "text-amber-400" : "text-gray-600"}">${p.favorite ? "★" : "☆"}</span>
               <span class="flex-1 truncate">${esc(p.name)}</span></button>`).join("")
          : `<div class="text-xs text-gray-500 p-1">No named people yet - type a name.</div>`;
        list.querySelectorAll("[data-i]").forEach(b => b.addEventListener("click", () => choose(shown[+b.dataset.i])));
      }
      function onKey(e) {
        if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); done(null); }
        else if (e.key === "Enter" && document.activeElement === q && shown.length) { e.preventDefault(); choose(shown[0]); }
      }
      document.addEventListener("keydown", onKey, true);
      host.addEventListener("click", e => { if (e.target === host) done(null); });
      host.querySelector("#cim_pp_cancel").addEventListener("click", () => done(null));
      host.querySelector("#cim_pp_remove")?.addEventListener("click", () => done({ remove: true }));
      q.addEventListener("input", render);
      q.focus();
      fetch("/api/persons/directory").then(r => r.json())
        .then(d => { people = orderPeople((d && d.people) || []); render(); })
        .catch(() => { people = []; render(); });
    });
  }
  /** @brief cimButton() when the core has it (it always does in the app). */
  function cimButtonHtml(o) {
    return typeof window.cimButton === "function" ? window.cimButton({ size: "sm", ...o })
      : `<button type="button" id="${esc(o.id)}">${esc(o.label)}</button>`;
  }

  /** @brief Pick a person for one face and apply it.
   *  @param target {filename, face_id} or {filename, region: {cx, cy, w, h}}
   *  @return the server result ({success, name, cluster_id, ...}), or null when cancelled.
   */
  async function changeFace(target, opts) {
    const choice = await pick({ allowRemove: true, ...(opts || {}) });
    if (!choice) return null;
    const body = { filename: target.filename, face_id: target.face_id, region: target.region };
    const res = choice.remove ? await post("/api/faces/unassign", body)
                              : await post("/api/faces/assign", { ...body, ...choice });
    if (res && res.success) {
      if (choice.remove) res.name = "";
      faceCache.delete(target.filename);
      window.dispatchEvent(new CustomEvent("cim:people-changed", { detail: { filename: target.filename, ...res } }));
    } else if (typeof showToast === "function") showToast((res && res.error) || "Could not change the person");
    return res;
  }

  /** @brief "Change person" on currentRegions[i] of the open picture; mirrors the result locally
   *  so the editor's next autosave writes the same name the server just wrote. */
  async function changeRegion(i) {
    const regs = typeof currentRegions !== "undefined" ? currentRegions : [];
    const r = regs[i], fn = window.currentFile;
    if (!r || !fn) return null;
    const res = await changeFace({ filename: fn, region: { cx: r.cx, cy: r.cy, w: r.w, h: r.h } });
    if (res && res.success && window.currentFile === fn && currentRegions[i] === r) {
      r.region_name = res.name || "";
      if (res.name) r.confirmed = true;
      if (typeof renderRegionsList === "function") renderRegionsList();
      if (typeof selectedRegionIdx !== "undefined" && selectedRegionIdx === i && typeof renderRegionEditor === "function") renderRegionEditor();
      if (window.CIMSimpleViewer && window.CIMSimpleViewer.active) window.CIMSimpleViewer.refreshPanels();
    }
    return res;
  }

  /** @brief Show every photo of a person in the gallery (person:<cluster>). */
  function search(clusterId) {
    if (clusterId == null || clusterId < 0) return;
    if (typeof closePopout === "function" && document.getElementById("popout_modal")
        && !document.getElementById("popout_modal").classList.contains("hidden")) closePopout();
    const si = document.getElementById("search_input");
    if (si) { si.value = "person:" + clusterId; si.dispatchEvent(new Event("input", { bubbles: true })); }
    if (typeof setPane === "function") setPane("gallery");
  }

  /** @brief who: {uuid} | {cluster_id}. */
  const setFavorite = (who, on) => post("/api/persons/favorite", { ...who, on: !!on });
  const setHidden = (who, hidden) => post("/api/persons/hide", { ...who, hidden: !!hidden });

  /** @brief A "change person" button on every face row of the viewer's region list. */
  function hookRegionList() {
    if (typeof window.renderRegionsList !== "function" || window.renderRegionsList.__peopleHooked) return;
    const orig = window.renderRegionsList;
    const wrapped = function () {
      const r = orig.apply(this, arguments);
      try { decorateRegionList(); } catch (e) { /* never break the editor */ }
      return r;
    };
    wrapped.__peopleHooked = true;
    window.renderRegionsList = wrapped;
  }
  function decorateRegionList() {
    const el = document.getElementById("regions_list");
    if (!el || (typeof isVideoFile === "function" && isVideoFile(window.currentFile))) return;
    const regs = typeof currentRegions !== "undefined" ? currentRegions : [];
    [...el.querySelectorAll(".rrow")].forEach((row, i) => {
      const r = regs[i];
      if (!isFace(r) || row.querySelector(".region-person")) return;
      const b = document.createElement("button");
      b.type = "button";
      b.className = "region-person text-blue-300 px-1 flex-shrink-0";
      b.dataset.feature = "tab.faces";
      b.setAttribute("data-write-gate", "tab.faces");   // reassigning needs write on the People tab
      b.title = `Change person${r.region_name ? " (now " + r.region_name + ")" : ""}`;
      b.textContent = "👤";
      b.addEventListener("click", e => { e.stopPropagation(); changeRegion(i); });
      row.insertBefore(b, row.querySelector(".region-del"));
    });
    if (window.CIMFeatures) window.CIMFeatures.apply(el);
  }
  hookRegionList();
  if (document.readyState === "loading") window.addEventListener("DOMContentLoaded", hookRegionList);

  window.CIMPeople = { facesIn, matchFace, isFace, cropStyle, pick, changeFace, changeRegion, search,
                       setFavorite, setHidden, orderPeople, forget: fn => faceCache.delete(fn) };
})();
