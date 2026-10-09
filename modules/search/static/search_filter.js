/* Search & Sort: the filter builder. A funnel icon next to the sort icon in the
 * gallery search box opens a form (text, dates, people, tags, rating, media
 * kind, camera, location, favorites, archived, orientation, size). Applying it
 * writes search tokens into the box, which stays the single source of truth:
 * opening the form parses the box back into the fields, and every token the
 * form does not understand (sort:, tag:, person:, a range it can't show...) is
 * kept exactly as typed. Fields whose token no enabled module registers (read
 * from /api/info) stay hidden. */
(function () {
  const DATE_KEYS = ["date", "datetime", "dateoriginal", "capture_date", "capturedate", "datedigitized", "modified"];
  const DATE_FIELDS = [["date", "Any date"], ["datetime", "Actual"], ["dateoriginal", "Original"],
    ["capture_date", "Capture"], ["datedigitized", "Digitized"], ["modified", "Modified"]];
  const KINDS = [["", "Any"], ["photo", "Photos"], ["video", "Videos"], ["raw", "From camera raw"]];
  const ORIENTS = [["", "Any"], ["portrait", "Portrait"], ["landscape", "Landscape"], ["square", "Square"],
    ["wide", "Wide"], ["ultrawide", "Ultra-wide"], ["tall", "Tall"]];
  const DAY = "\\d{4}-\\d{2}-\\d{2}";
  const DATE_RE = new RegExp(`^(${DATE_KEYS.join("|")}):(>=|<=)?(${DAY})(?:\\.\\.(${DAY}))?$`, "i");
  const DIM_RE = /^(width|height|min|max):?(<=|>=|<|>|=)\d+$/i;

  let prefixes = null;          // Set of search-token prefixes this install answers ("fav:")
  let infoPromise = null;
  const suggested = {};         // datalist id -> true once filled

  const words = s => String(s || "").split(/\s+/).filter(Boolean);
  const list = s => String(s || "").split(",").map(x => x.trim()).filter(Boolean);
  // A token can't hold a space: '*' (a wildcard for name: / tags:) bridges one.
  const squash = s => s.trim().replace(/\s+/g, "*");
  const $ = id => document.getElementById(id);

  /** @brief An empty builder state. */
  function blank() {
    return { text: [], dateField: "date", from: "", to: "", people: [], tagsIn: [], tagsOut: [],
             rating: "", kind: "", camera: "", location: "", near: "", fav: false, orient: "", minSide: "" };
  }

  /** @brief Fold one token into `st`. @return true when the builder owns it. */
  function consume(st, tok) {
    const low = tok.toLowerCase();
    if (!tok.includes(":") && !DIM_RE.test(tok)) { st.text.push(tok); return true; }
    let m = low.match(/^min:?>=(\d+)$/);
    if (m && !st.minSide) { st.minSide = m[1]; return true; }
    m = tok.match(DATE_RE);
    if (m && !st.from && !st.to) {
      const [, field, op, a, b] = m;
      if (b && op) return false;
      st.dateField = field.toLowerCase() === "capturedate" ? "capture_date" : field.toLowerCase();
      if (b) { st.from = a; st.to = b; }
      else if (op === ">=") st.from = a;
      else if (op === "<=") st.to = a;
      else { st.from = a; st.to = a; }
      return true;
    }
    const i = tok.indexOf(":"), key = low.slice(0, i), val = tok.slice(i + 1);
    if (key === "name" || key === "tags") {
      const terms = list(val);
      if (!terms.length || val.includes("|")) return false;
      if (key === "name") {
        if (terms.some(t => t.startsWith("-"))) return false;
        st.people.push(...terms.map(t => t.replace(/^\+/, "")));
      } else {
        terms.forEach(t => (t.startsWith("-") ? st.tagsOut : st.tagsIn).push(t.replace(/^[+-]/, "")));
      }
      return true;
    }
    if (key === "rating" && /^>=\d+$/.test(val) && !st.rating) { st.rating = val.slice(2); return true; }
    if (key === "kind" && !st.kind && KINDS.some(k => k[0] && k[0] === val.toLowerCase())) {
      st.kind = val.toLowerCase(); return true;
    }
    m = tok.match(/^exif:model:(.+)$/i);
    if (m && !st.camera) { st.camera = m[1].replace(/%/g, " "); return true; }
    if (key === "location" && val && !st.location) { st.location = val.replace(/\*/g, " "); return true; }
    if (key === "near" && val && !st.near) { st.near = val; return true; }
    if (key === "fav" && ["", "yes", "me"].includes(val.toLowerCase())) { st.fav = true; return true; }
    if (key === "ratio" && !st.orient && ORIENTS.some(o => o[0] && o[0] === val.toLowerCase())) {
      st.orient = val.toLowerCase(); return true;
    }
    return false;
  }

  /** @brief Split a query into builder state and the tokens it leaves alone.
   *  @return {st, keep, semantic}. */
  function parse(q) {
    const st = blank(), keep = [];
    q = String(q || "").trim();
    // A semantic query (sem: / ~) is one sentence, not tokens: keep it whole.
    if (/^(sem:|~)/i.test(q)) return { st, keep: [q], semantic: true };
    for (const tok of words(q)) if (!consume(st, tok)) keep.push(tok);
    return { st, keep, semantic: false };
  }

  /** @brief The builder's own tokens for a state. */
  function tokensFor(st) {
    const out = [];
    const f = st.dateField || "date";
    if (st.from && st.to) out.push(st.from === st.to ? `${f}:${st.from}` : `${f}:${st.from}..${st.to}`);
    else if (st.from) out.push(`${f}:>=${st.from}`);
    else if (st.to) out.push(`${f}:<=${st.to}`);
    if (st.people.length) out.push("name:" + st.people.map(squash).join(","));
    const tags = [...st.tagsIn.map(t => "+" + squash(t)), ...st.tagsOut.map(t => "-" + squash(t))];
    if (tags.length) out.push("tags:" + tags.join(","));
    if (st.rating) out.push(`rating:>=${st.rating}`);
    if (st.kind) out.push(`kind:${st.kind}`);
    if (st.camera.trim()) out.push("exif:Model:" + st.camera.trim().replace(/\s+/g, "%"));
    if (st.location.trim()) out.push("location:" + squash(st.location));
    if (st.near.trim()) out.push("near:" + st.near.replace(/\s+/g, ""));
    if (st.fav) out.push("fav:yes");
    if (st.orient) out.push(`ratio:${st.orient}`);
    if (st.minSide) out.push(`min>=${parseInt(st.minSide, 10)}`);
    return out;
  }

  /** @brief The full query: free text, the kept tokens in their order, then the builder's. */
  function compose(st, keep) {
    return [...st.text, ...keep, ...tokensFor(st)].join(" ").trim();
  }

  /** @brief Read the form into a state. */
  function readForm() {
    const v = id => ($(id) ? $(id).value : "");
    const st = blank();
    st.text = words(v("sf_text"));
    st.dateField = v("sf_date_field") || "date";
    st.from = v("sf_from"); st.to = v("sf_to");
    st.people = list(v("sf_people"));
    st.tagsIn = list(v("sf_tags_in")); st.tagsOut = list(v("sf_tags_out"));
    st.rating = v("sf_rating"); st.kind = v("sf_kind"); st.camera = v("sf_camera");
    st.location = v("sf_location"); st.near = v("sf_near");
    st.fav = !!($("sf_fav") && $("sf_fav").checked);
    st.orient = v("sf_orient");
    st.minSide = /^\d+$/.test(v("sf_min").trim()) && +v("sf_min") > 0 ? v("sf_min").trim() : "";
    return st;
  }

  /** @brief Fill the form from a state. */
  function fillForm(st) {
    const set = (id, val) => { if ($(id)) $(id).value = val; };
    set("sf_text", st.text.join(" "));
    set("sf_date_field", st.dateField); set("sf_from", st.from); set("sf_to", st.to);
    set("sf_people", st.people.join(", "));
    set("sf_tags_in", st.tagsIn.join(", ")); set("sf_tags_out", st.tagsOut.join(", "));
    set("sf_rating", st.rating); set("sf_kind", st.kind); set("sf_camera", st.camera);
    set("sf_location", st.location); set("sf_near", st.near);
    if ($("sf_fav")) $("sf_fav").checked = !!st.fav;
    if ($("sf_archived")) $("sf_archived").checked = typeof galleryView !== "undefined" && galleryView === "archive";
    set("sf_orient", st.orient); set("sf_min", st.minSide);
  }

  /** @brief Token prefixes from /api/info's "search" section (loaded once). */
  function loadInfo() {
    if (!infoPromise) {
      infoPromise = fetch("/api/info").then(r => r.json()).then(d => {
        const set = new Set();
        for (const sec of (d && d.sections) || []) {
          if (sec.id !== "search") continue;
          for (const row of sec.rows || []) {
            for (const m of String(row.token || "").matchAll(/(?:^|[\s/])-?([a-z_]+):/gi)) set.add(m[1].toLowerCase() + ":");
          }
        }
        prefixes = set;
      }).catch(() => { prefixes = new Set(); });
    }
    return infoPromise;
  }

  /** @brief Show the rows whose tokens exist; fetch suggestions for the visible ones. */
  function applyVisibility() {
    const pop = $("sf_pop");
    if (!pop) return;
    pop.querySelectorAll("[data-sf-need]").forEach(row => {
      const needs = row.dataset.sfNeed.split(" ");
      let ok = needs.every(n => n === "js:rating" ? typeof window.ratingSet === "function"
        : n === "view:archive" ? !!(window._galleryViews && window._galleryViews.archive)
        : !!(prefixes && prefixes.has(n)));
      row.classList.toggle("hidden", !ok);
    });
    if (prefixes && prefixes.has("person:")) suggest("sf_people_list", "/api/persons/directory",
      d => (d.people || []).map(p => p.name));
    if (prefixes && prefixes.has("exif:")) suggest("sf_camera_list", "/api/search_sort/values?ns=exif&tag=Model",
      d => (d.values || []).map(v => v.value));
  }

  /** @brief Fill a datalist from an endpoint once. */
  function suggest(listId, url, pick) {
    if (suggested[listId]) return;
    suggested[listId] = true;
    fetch(url).then(r => r.json()).then(d => {
      const dl = $(listId);
      if (!dl || !d || d.success === false) return;
      dl.innerHTML = pick(d).slice(0, 200).map(v => `<option value="${_esc(v)}"></option>`).join("");
    }).catch(() => { });
  }

  /** @brief Light the icon while the box carries builder tokens. */
  function sync() {
    const btn = $("sf_btn"), si = $("search_input");
    if (!btn || !si) return;
    const on = tokensFor(parse(si.value).st).length > 0;
    btn.classList.toggle("sf-on", on);
    btn.title = on ? "Filters (active)" : "Filters";
  }

  /** @brief Show or hide the form; opening refills it from the box. */
  function toggle(force) {
    const pop = $("sf_pop"), si = $("search_input");
    if (!pop || !si) return;
    const open = force !== undefined ? !!force : pop.classList.contains("hidden");
    pop.classList.toggle("hidden", !open);
    if (!open) return;
    if (typeof hideQuickFilters === "function") hideQuickFilters();
    const p = parse(si.value);
    fillForm(p.st);
    if ($("sf_sem_note")) $("sf_sem_note").classList.toggle("hidden", !p.semantic);
    applyVisibility();
    loadInfo().then(applyVisibility);
  }

  /** @brief Write the form into the search box (keeping foreign tokens) and search. */
  function apply() {
    const si = $("search_input");
    if (!si) return;
    const p = parse(si.value), st = readForm();
    // Filters don't apply to a semantic ranking: a filled form replaces it.
    if (!p.semantic || st.text.length || tokensFor(st).length) si.value = compose(st, p.semantic ? [] : p.keep);
    const arc = $("sf_archived");
    if (arc && !arc.closest(".hidden") && typeof setGalleryView === "function" && typeof galleryView !== "undefined") {
      const want = arc.checked ? "archive" : (galleryView === "archive" ? "grid" : galleryView);
      if (want !== galleryView) setGalleryView(want);
    }
    toggle(false);
    si.dispatchEvent(new Event("input", { bubbles: true }));
    sync();
  }

  /** @brief Drop every builder-owned token from the box (foreign ones stay) and search. */
  function clear() {
    const si = $("search_input");
    if (!si) return;
    fillForm(blank());
    si.value = compose(blank(), parse(si.value).keep);
    toggle(false);
    si.dispatchEvent(new Event("input", { bubbles: true }));
    sync();
  }

  const opts = arr => arr.map(([v, l]) => `<option value="${v}">${l}</option>`).join("");
  const inp = "w-full bg-gray-700 rounded border border-gray-600 px-1.5 py-0.5 text-xs text-gray-100";
  const row = (label, body, need) =>
    `<div class="sf-row"${need ? ` data-sf-need="${need}"` : ""}><label class="sf-label">${label}</label>${body}</div>`;
  const funnel = `<svg viewBox="0 0 16 16" width="12" height="12" fill="currentColor" aria-hidden="true">
      <path d="M1.5 2h13l-5 6v5l-3 1.5V8z"/></svg>`;

  registerControlButton("search_tools",
    `<span class="relative flex items-center" id="sf_wrap">
       <button type="button" id="sf_btn" title="Filters" aria-label="Filters"
         class="cim-btn cim-btn-neutral cim-btn-xs text-gray-300">${funnel}</button>
       <div id="sf_pop" class="hidden absolute right-0 top-full mt-2 z-30 bg-gray-800 border border-gray-600 rounded shadow-lg p-2 w-72 flex flex-col gap-1.5 text-xs text-gray-200">
         <div id="sf_sem_note" class="hidden text-amber-300">The box holds a semantic search (sem: / ~), which ignores filters; applying filters replaces it.</div>
         ${row("Text", `<input id="sf_text" type="text" class="${inp}" placeholder="words in name, tags, description">`)}
         ${row("Date", `<div class="flex flex-col gap-1">
             <select id="sf_date_field" class="${inp}">${opts(DATE_FIELDS)}</select>
             <div class="flex gap-1 items-center"><input id="sf_from" type="date" class="${inp}" title="From">
               <span class="text-gray-500">-</span><input id="sf_to" type="date" class="${inp}" title="To"></div></div>`)}
         ${row("People", `<input id="sf_people" type="text" list="sf_people_list" class="${inp}" placeholder="alice, bob (all of)">
             <datalist id="sf_people_list"></datalist>`, "name:")}
         ${row("Tags", `<div class="flex flex-col gap-1">
             <input id="sf_tags_in" type="text" class="${inp}" placeholder="with: cat, outdoor">
             <input id="sf_tags_out" type="text" class="${inp}" placeholder="without: dog"></div>`, "tags:")}
         ${row("Rating", `<select id="sf_rating" class="${inp}">${opts([["", "Any"], ["1", "1+ stars"], ["2", "2+ stars"],
             ["3", "3+ stars"], ["4", "4+ stars"], ["5", "5 stars"]])}</select>`, "rating: js:rating")}
         ${row("Media", `<select id="sf_kind" class="${inp}">${opts(KINDS)}</select>`, "kind:")}
         ${row("Camera", `<input id="sf_camera" type="text" list="sf_camera_list" class="${inp}" placeholder="model contains">
             <datalist id="sf_camera_list"></datalist>`, "exif:")}
         ${row("Place", `<input id="sf_location" type="text" class="${inp}" placeholder="place name">`, "location:")}
         ${row("Near", `<input id="sf_near" type="text" class="${inp}" placeholder="lat,lon,km">`, "near:")}
         ${row("Orientation", `<select id="sf_orient" class="${inp}">${opts(ORIENTS)}</select>`, "ratio:")}
         ${row("Short side", `<input id="sf_min" type="number" min="0" step="1" class="${inp}" placeholder="at least (px)">`)}
         <div class="flex gap-3 flex-wrap">
           <label class="flex items-center gap-1 hidden" data-sf-need="fav:"><input id="sf_fav" type="checkbox"> Favorites</label>
           <label class="flex items-center gap-1 hidden" data-sf-need="view:archive" title="Show the Archive view"><input id="sf_archived" type="checkbox"> Archived</label>
         </div>
         <div class="flex justify-end gap-1 pt-1 border-t border-gray-700">
           ${cimButton({ label: "Clear", id: "sf_clear", variant: "neutral", size: "xs" })}
           ${cimButton({ label: "Apply", id: "sf_apply", variant: "primary", size: "xs" })}
         </div>
       </div>
     </span>`);

  /** @brief Keep the box's text clear of the icons inside it. */
  function pad() {
    const si = $("search_input");
    const tools = si && si.parentElement && si.parentElement.querySelector('[data-ext-area="search_tools"]');
    if (tools && tools.offsetWidth > 0) si.style.paddingRight = (tools.offsetWidth + 8) + "px";
  }

  document.addEventListener("click", e => {
    const t = e.target;
    if (!(t instanceof Element)) return;
    if (t.closest("#sf_btn")) { toggle(); return; }
    if (t.closest("#sf_apply")) { apply(); return; }
    if (t.closest("#sf_clear")) { clear(); return; }
    const pop = $("sf_pop");
    if (pop && !pop.classList.contains("hidden") && !t.closest("#sf_pop")) toggle(false);
  });
  document.addEventListener("keydown", e => {
    if (!(e.target instanceof Element) || !e.target.closest("#sf_pop")) return;
    if (e.key === "Enter") { e.preventDefault(); apply(); }
    else if (e.key === "Escape") toggle(false);
  });
  document.addEventListener("input", e => { if (e.target.id === "search_input") sync(); });
  window.addEventListener("resize", pad);
  setTimeout(pad, 0);
  sync();
})();
