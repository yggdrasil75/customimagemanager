/* Map module front-end.
 *
 * Registers the "Map" left tab. Leaflet + MarkerCluster are loaded lazily the
 * first time the tab (or a minimap) needs them, from /api/map/vendor/ (served
 * out of the pip XStatic packages), so a page that never shows a map never
 * pays for it.
 *
 * Markers are thumbnails; clusters show the newest-in-cluster thumbnail and a
 * count. Clicking a marker opens the file in the editor (selectFile). The open
 * file is highlighted and, if it has a position, centred while the tab shows.
 * The toolbar's date range and "use the gallery search" switch scope the
 * markers through /api/map/points?q=&folder=&album=&from=&to=.
 *
 * Also here: the "Places" gallery view (countries, regions, cities with
 * counts; a click searches location:) and the editor panel's minimap of the
 * open file (click: the Map tab, centred there).
 */
(function () {
  "use strict";

  const VENDOR = "/api/map/vendor/";
  const S = {
    loading: null,       // Promise for the lazy Leaflet load
    map: null,
    cluster: null,
    points: [],          // [[rel, lat, lon, isVideo, approximate], ...]
    byRel: new Map(),    // rel -> L.Marker
    current: null,       // L.CircleMarker ring on the open file
    currentRel: null,
    fitted: false,
    poll: null,
    scanFinished: 0,     // scan.finished of the set on screen: a new pass => rebuild
    scopeKey: "",        // the filter the markers on screen were fetched with
    pendingCentre: null, // [lat, lon, rel] to show once the map is up (minimap click)
  };

  /** @brief HTML-escape a string. */
  function esc(s) {
    return String(s).replace(/[&<>"']/g, c =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  /** @brief A file's thumbnail URL. */
  function thumbUrl(rel) { return "/api/thumb/" + encodeURIComponent(rel); }
  /** @brief Show a toast when the core offers one. */
  function toast(msg) { if (typeof showToast === "function") showToast(msg); }

  /** @brief Add a stylesheet once; resolves when it loaded. */
  function loadCss(href) {
    return new Promise((res, rej) => {
      if (document.querySelector(`link[href="${href}"]`)) return res();
      const l = document.createElement("link");
      l.rel = "stylesheet"; l.href = href;
      l.onload = res; l.onerror = () => rej(new Error("failed to load " + href));
      document.head.appendChild(l);
    });
  }
  /** @brief Add a script once; resolves when it ran. */
  function loadJs(src) {
    return new Promise((res, rej) => {
      if (document.querySelector(`script[src="${src}"]`)) return res();
      const s = document.createElement("script");
      s.src = src; s.async = false;
      s.onload = res; s.onerror = () => rej(new Error("failed to load " + src));
      document.head.appendChild(s);
    });
  }
  /** @brief Load Leaflet + MarkerCluster once (lazily). */
  function loadLeaflet() {
    if (!S.loading) {
      S.loading = Promise.all([
        loadCss(VENDOR + "leaflet/leaflet.css"),
        loadCss(VENDOR + "markercluster/MarkerCluster.css"),
        loadCss(VENDOR + "markercluster/MarkerCluster.Default.css"),
      ]).then(() => loadJs(VENDOR + "leaflet/leaflet.js"))
        .then(() => loadJs(VENDOR + "markercluster/leaflet.markercluster.js"))
        .catch(e => { S.loading = null; throw e; });
    }
    return S.loading;
  }

  // -- markers -----------------------------------------------------------
  /** @brief A file's thumbnail marker icon (dashed when placed from a typed city, no GPS). */
  function photoIcon(rel, video, approx) {
    return L.divIcon({
      className: "cim-map-pin",
      html: `<div class="cim-map-thumb${video ? " is-video" : ""}${approx ? " is-approx" : ""}">` +
            `<img loading="lazy" src="${esc(thumbUrl(rel))}" alt="">` +
            (video ? '<span class="cim-map-play">▶</span>' : "") + "</div>",
      iconSize: [48, 48],
      iconAnchor: [24, 24],
    });
  }

  /** @brief A cluster icon: one member thumbnail and the count. */
  function clusterIcon(c) {
    const kids = c.getAllChildMarkers();
    const n = c.getChildCount();
    const rel = kids.length ? kids[0].options.cimRel : "";
    const size = n < 10 ? 52 : n < 100 ? 58 : n < 1000 ? 64 : 70;
    return L.divIcon({
      className: "cim-map-pin",
      html: `<div class="cim-map-thumb cim-map-cluster" style="width:${size}px;height:${size}px">` +
            (rel ? `<img loading="lazy" src="${esc(thumbUrl(rel))}" alt="">` : "") +
            `<span class="cim-map-count">${n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + "k" : n}</span></div>`,
      iconSize: [size, size],
      iconAnchor: [size / 2, size / 2],
    });
  }

  /** @brief Open a file in the editor. */
  function openFile(rel) {
    if (typeof selectFile === "function") selectFile(rel);
  }

  /** @brief Rebuild every marker from S.points. */
  function buildMarkers() {
    S.cluster.clearLayers();
    S.byRel.clear();
    const ms = [];
    for (const [rel, lat, lon, video, approx] of S.points) {
      const m = L.marker([lat, lon], { icon: photoIcon(rel, !!video, !!approx), cimRel: rel,
                                       title: approx ? rel + " (approximate: from its city, no GPS)" : rel });
      m.on("click", () => openFile(rel));
      S.byRel.set(rel, m);
      ms.push(m);
    }
    S.cluster.addLayers(ms);
  }

  /** @brief The toolbar count and scan progress. */
  function updateCount(scan) {
    const el = document.getElementById("map_count");
    if (el) el.textContent = `${S.points.length.toLocaleString()} geotagged`;
    const sc = document.getElementById("map_scan");
    if (sc) sc.textContent = scan && scan.running
      ? `reading locations... ${scan.done.toLocaleString()}/${scan.total.toLocaleString()}` : "";
  }

  /** @brief Query string for the markers: date range + (optionally) the gallery's search. */
  function scopeParams() {
    const p = new URLSearchParams();
    const useSearch = document.getElementById("map_use_search");
    if (useSearch && useSearch.checked && typeof galleryQuery === "function") {
      const g = galleryQuery();
      if (g.q) p.set("q", g.q);
      if (g.folder) p.set("folder", g.folder);
      if (g.album) p.set("album", g.album);
      if (g.recursive) p.set("recursive", "1");
    }
    const from = document.getElementById("map_from"), to = document.getElementById("map_to");
    if (from && from.value) p.set("from", from.value);
    if (to && to.value) p.set("to", to.value);
    return p.toString();
  }

  /** @brief Fetch the scoped markers (and tile settings). */
  async function fetchPoints() {
    const qs = scopeParams();
    const d = await fetch("/api/map/points" + (qs ? "?" + qs : "")).then(r => r.json());
    if (!d.success) throw new Error(d.error || "map points failed");
    d.scopeKey = qs;
    return d;
  }

  /** @brief Poll again while a scan is running. */
  function schedulePoll(scan) {
    clearTimeout(S.poll);
    if (scan && scan.running) S.poll = setTimeout(reload, 4000);
  }

  /** @brief Re-fetch the markers; rebuild when the set changed. */
  async function reload() {
    try {
      const d = await fetchPoints();
      const rescoped = d.scopeKey !== S.scopeKey;
      const changed = rescoped || d.points.length !== S.points.length ||
                      (d.scan && d.scan.finished !== S.scanFinished);
      S.scanFinished = d.scan ? d.scan.finished : 0;
      S.scopeKey = d.scopeKey;
      S.points = d.points;
      if (changed) buildMarkers();
      updateCount(d.scan);
      if (changed && (!S.fitted || rescoped) && !S.pendingCentre) mapFitAll();
      applyPendingCentre();
      schedulePoll(d.scan);
    } catch (e) { console.error(e); }
  }

  // -- map setup ---------------------------------------------------------
  /** @brief Create the map the first time the tab shows. */
  async function init() {
    await loadLeaflet();
    const d = await fetchPoints();
    const host = document.getElementById("map_canvas");
    if (!host) return;
    S.map = L.map(host, { worldCopyJump: true, zoomControl: true, preferCanvas: false })
      .setView([20, 0], 2);
    L.tileLayer(d.tiles.url, {
      maxZoom: d.tiles.max_zoom || 19,
      attribution: d.tiles.attribution || "",
      // The app sends Referrer-Policy: same-origin (modules/auth), so tile
      // requests to another host would carry no Referer, and OSM's tile usage
      // policy blocks those. Send the origin only, never the page path.
      referrerPolicy: "strict-origin-when-cross-origin",
    }).addTo(S.map);
    S.cluster = L.markerClusterGroup({
      chunkedLoading: true,
      showCoverageOnHover: false,
      spiderfyOnMaxZoom: true,
      maxClusterRadius: 60,
      iconCreateFunction: clusterIcon,
    });
    S.map.addLayer(S.cluster);
    S.points = d.points;
    S.scanFinished = d.scan ? d.scan.finished : 0;
    S.scopeKey = d.scopeKey;
    buildMarkers();
    updateCount(d.scan);
    schedulePoll(d.scan);
    if (window.ResizeObserver) new ResizeObserver(() => S.map && S.map.invalidateSize()).observe(host);
    if (window.CIMFeatures && !window.CIMFeatures.canWrite("tab.map"))
      document.getElementById("map_rescan_btn")?.classList.add("hidden");
    mapFitAll();
    if (window.currentFile) highlight(window.currentFile, false);
    applyPendingCentre();
  }

  /** @brief Centre on the spot a minimap click asked for, once the map exists. */
  function applyPendingCentre() {
    if (!S.map || !S.pendingCentre) return;
    const [lat, lon, rel] = S.pendingCentre;
    S.pendingCentre = null;
    S.map.setView([lat, lon], Math.max(S.map.getZoom(), 15));
    if (rel) highlight(rel, false);
  }

  /** @brief Open the Map tab centred on a position (the minimap's click). */
  function mapOpenAt(lat, lon, rel) {
    S.pendingCentre = [+lat, +lon, rel || ""];
    if (typeof setPane === "function") setPane("map");
    applyPendingCentre();
  }

  /** @brief Map tab shown: create or refresh the map. */
  async function onShow() {
    try {
      if (!S.map) await init();
      else {
        S.map.invalidateSize();
        reload();
      }
    } catch (e) {
      console.error(e);
      const el = document.getElementById("map_count");
      if (el) el.textContent = "Map failed to load: " + e.message;
    }
  }

  /** @brief Is the Map tab on screen? */
  function mapVisible() {
    const pane = document.getElementById("map_pane");
    return !!(S.map && pane && !pane.classList.contains("hidden"));
  }

  // -- open-file highlight -----------------------------------------------
  /** @brief Ring the open file on the map (and pan to it). */
  async function highlight(rel, pan) {
    if (!S.map) return;
    S.currentRel = rel;
    if (S.current) { S.map.removeLayer(S.current); S.current = null; }
    let ll = null;
    const m = S.byRel.get(rel);
    if (m) ll = m.getLatLng();
    else {
      // Not in the loaded set: maybe its GPS was just edited. Ask (refreshes the cache).
      try {
        const d = await fetch("/api/map/file?filename=" + encodeURIComponent(rel)).then(r => r.json());
        if (S.currentRel !== rel) return;
        if (d.success && d.lat != null) ll = L.latLng(d.lat, d.lon);
      } catch (e) { /* ignore */ }
    }
    if (!ll) return;
    S.current = L.circleMarker(ll, { radius: 30, color: "#3b82f6", weight: 3, fill: false, interactive: false })
      .addTo(S.map);
    if (pan) {
      if (m) S.cluster.zoomToShowLayer(m, () => {});
      else S.map.panTo(ll);
    }
  }

  if (window.registerFileMetaHook) {
    registerFileMetaHook((meta, rel) => {
      if (rel && mapVisible()) highlight(rel, true);
      else if (rel) S.currentRel = rel;
    });
  }

  // -- toolbar actions ---------------------------------------------------
  /** @brief Zoom to show every marker. */
  function mapFitAll() {
    if (!S.map) return;
    if (!S.points.length) { S.map.setView([20, 0], 2); return; }
    const b = L.latLngBounds(S.points.map(p => [p[1], p[2]]));
    S.map.fitBounds(b.pad(0.1), { maxZoom: 14 });
    S.fitted = true;
  }

  /** @brief Put `q` into the gallery search box and run it on the grid. */
  function runGallerySearch(q) {
    const si = document.getElementById("search_input");
    if (si) si.value = q;
    if (typeof setPane === "function") setPane("gallery");
    try { currentSearch = q; currentPage = 0; } catch (e) { /* globals.js not loaded */ }
    if (typeof setGalleryView === "function" && typeof galleryView !== "undefined" && galleryView !== "grid")
      setGalleryView("grid");
    else if (typeof loadGallery === "function") loadGallery();
  }

  /** @brief Search tokens of a query, a "quoted value" kept whole (as the server splits them). */
  function searchTokens(q) {
    return String(q || "").match(/(?:[^\s"]+|"[^"]*"?)+/g) || [];
  }

  /** @brief The text in the gallery search box. */
  function currentQuery() {
    const si = document.getElementById("search_input");
    return (si ? si.value : (typeof currentSearch !== "undefined" ? currentSearch : "")) || "";
  }

  /** @brief Filter the gallery to the area on screen (bbox: token). */
  function mapShowInGallery() {
    if (!S.map) return;
    const b = S.map.getBounds();
    const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
    const wrap = v => ((v + 540) % 360) - 180;
    let w = b.getWest(), e = b.getEast();
    if (e - w >= 360) { w = -180; e = 180; } else { w = wrap(w); e = wrap(e); }
    const s = clamp(b.getSouth(), -90, 90), n = clamp(b.getNorth(), -90, 90);
    const f = v => (+v.toFixed(5)).toString();
    const tok = `bbox:${f(s)},${f(w)},${f(n)},${f(e)}`;
    runGallerySearch(searchTokens(currentQuery()).filter(t => !/^bbox:/i.test(t)).concat(tok).join(" "));
  }

  /** @brief Re-fetch the markers after the date range / search switch changed. */
  function mapFilterChanged() {
    if (S.map) reload();
  }

  /** @brief Clear the date range. */
  function mapClearDates() {
    for (const id of ["map_from", "map_to"]) {
      const el = document.getElementById(id);
      if (el) el.value = "";
    }
    mapFilterChanged();
  }

  /** @brief Re-read every position from the files. */
  async function mapRescan() {
    try {
      const d = await fetch("/api/map/rescan", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ force: true }),
      }).then(r => r.json());
      if (!d.success) { toast(d.error || "Rescan failed"); return; }
      toast("Re-reading locations...");
      updateCount({ running: true, done: 0, total: (d.scan && d.scan.total) || 0 });
      clearTimeout(S.poll);
      S.poll = setTimeout(reload, 1500);
    } catch (e) { toast("Rescan failed: " + e.message); }
  }

  // -- Places gallery view -----------------------------------------------
  const P = { host: null, ctx: null, gen: 0 };

  /** @brief A location: token for a place path (names joined with " / " keep their commas). */
  function locationToken(parts) {
    const clean = parts.filter(Boolean).map(p => String(p).replace(/["\/]/g, " ").trim()).filter(Boolean);
    return clean.length ? `location:"${clean.join(" / ")}"` : "";
  }

  /** @brief Replace the search's location: tokens with `tok` and show the grid. */
  function searchPlace(tok) {
    if (!tok) return;
    runGallerySearch(searchTokens(currentQuery()).filter(t => !/^location:/i.test(t)).concat(tok).join(" "));
  }

  /** @brief A clickable place name (searches `tok`) and its count. */
  function placeLink(label, count, tok, cls) {
    return `<button type="button" class="map-place-link ${cls || ""}" data-loc="${esc(tok)}"` +
           ` title="Search ${esc(tok)}">${esc(label)}</button>` +
           `<span class="map-place-count">${Number(count).toLocaleString()}</span>`;
  }

  /** @brief Draw the countries / regions / cities tree of /api/map/places. */
  function renderPlaces(d) {
    if (!P.host) return;
    const body = P.host.querySelector(".map-places-body");
    const count = P.host.querySelector(".map-places-total");
    if (!d.success) {
      body.innerHTML = `<div class="map-places-empty">${esc(d.error || "Places unavailable")}</div>`;
      count.textContent = "";
      return;
    }
    count.textContent = `${Number(d.total).toLocaleString()} placed`;
    if (!d.countries.length) {
      body.innerHTML = '<div class="map-places-empty">No places yet: files need a GPS position ' +
        ((d.status && d.status.running) ? "(still resolving places...)" : "") + "</div>";
      return;
    }
    const open = d.countries.length <= 3;
    body.innerHTML = d.countries.map(c => {
      const regions = c.regions.map(r => {
        const cities = r.cities.filter(x => x.name).map(x =>
          `<li>${placeLink(x.name, x.count, locationToken([x.name, r.name, c.cc]), "map-place-city")}</li>`).join("");
        const head = r.name ? placeLink(r.name, r.count, locationToken([r.name, c.cc]), "map-place-region")
                            : `<span class="text-gray-500">(no region)</span><span class="map-place-count">${r.count}</span>`;
        return `<li class="map-place-region-row"><div class="map-place-row">${head}</div>` +
               (cities ? `<ul class="map-place-cities">${cities}</ul>` : "") + "</li>";
      }).join("");
      return `<details class="map-place-country"${open ? " open" : ""}><summary class="map-place-row">` +
             placeLink(c.name, c.count, locationToken([c.name]), "map-place-country-name") +
             (c.continent ? `<span class="map-place-cont">${esc(c.continent)}</span>` : "") +
             `</summary><ul class="map-place-regions">${regions}</ul></details>`;
    }).join("");
  }

  /** @brief Fetch the places tree for the gallery's current scope. */
  async function loadPlaces() {
    if (!P.host) return;
    const gen = ++P.gen;
    const p = new URLSearchParams();
    const ctx = P.ctx || {};
    for (const k of ["q", "folder", "album"]) if (ctx[k]) p.set(k, ctx[k]);
    if (ctx.recursive) p.set("recursive", "1");
    let d;
    try {
      d = await fetch("/api/map/places?" + p.toString()).then(r => r.json());
    } catch (e) { d = { success: false, error: e.message }; }
    if (gen === P.gen) renderPlaces(d);
  }

  const placesView = {
    id: "places",
    label: "Places",
    title: "Places: countries, regions and cities (click one to search it)",
    feature: "tab.map",
    mount(host, ctx) {
      P.host = host;
      P.ctx = ctx;
      host.innerHTML = '<div class="map-places"><div class="map-places-bar">' +
        '<span class="font-bold text-gray-300">Places</span><span class="map-places-total"></span></div>' +
        '<div class="map-places-body"><div class="map-places-empty">Loading...</div></div></div>';
      host.addEventListener("click", onPlaceClick);
      loadPlaces();
    },
    refresh(ctx) {
      P.ctx = ctx;
      loadPlaces();
    },
    unmount() {
      if (P.host) P.host.removeEventListener("click", onPlaceClick);
      P.gen++;
      P.host = null;
    },
  };

  /** @brief A click on a place name runs its location: search. */
  function onPlaceClick(e) {
    const b = e.target.closest("[data-loc]");
    if (!b) return;
    e.preventDefault();
    searchPlace(b.dataset.loc);
  }

  // -- viewer minimap ------------------------------------------------------
  const M = { box: null, map: null, layer: null, marker: null, tilesUrl: "", seq: 0, at: null };

  /** @brief May this user see the map (tab.map)? */
  function miniAllowed() {
    return !(window.CIMFeatures && window.CIMFeatures.allowed && !window.CIMFeatures.allowed("tab.map"));
  }

  /** @brief The minimap's box in the editor panel, created on first use. */
  function miniBox() {
    if (M.box && document.body.contains(M.box)) return M.box;
    const panel = document.getElementById("editor_panel");
    if (!panel) return null;
    const box = document.createElement("div");
    box.id = "map_minimap_box";
    box.className = "hidden flex flex-col gap-1";
    box.setAttribute("data-feature", "tab.map");
    box.innerHTML = '<div class="flex items-center gap-2 text-xs">' +
      '<span class="text-gray-400 font-bold uppercase">Location</span>' +
      '<span id="map_minimap_place" class="text-gray-300 truncate flex-1 min-w-0"></span></div>' +
      '<div id="map_minimap" class="cim-minimap" title="Open on the map"></div>';
    // after the album chips when they are there, else at the end
    let anchor = document.getElementById("album_chips");
    while (anchor && anchor.parentElement && anchor.parentElement !== panel) anchor = anchor.parentElement;
    if (anchor && anchor.parentElement === panel) panel.insertBefore(box, anchor.nextSibling);
    else panel.appendChild(box);
    box.querySelector("#map_minimap").addEventListener("click", () => {
      if (M.at) mapOpenAt(M.at[0], M.at[1], M.at[2]);
    });
    M.box = box;
    return box;
  }

  /** @brief "City, Region, Country" for a place row. */
  function placeText(p) {
    if (!p) return "";
    return [p.city, p.admin1 && p.admin1 !== p.city ? p.admin1 : "", p.country].filter(Boolean).join(", ");
  }

  /** @brief Draw (or move) the minimap once Leaflet is loaded. */
  async function drawMini(lat, lon, tiles) {
    await loadLeaflet();
    const el = document.getElementById("map_minimap");
    if (!el || !M.at || M.at[0] !== lat || M.at[1] !== lon) return;
    if (!M.map || M.map.getContainer() !== el) {
      M.map = L.map(el, { zoomControl: false, attributionControl: false, dragging: false,
                          scrollWheelZoom: false, doubleClickZoom: false, boxZoom: false,
                          keyboard: false, touchZoom: false });
      M.layer = null;
    }
    if (!M.layer || M.tilesUrl !== tiles.url) {
      if (M.layer) M.map.removeLayer(M.layer);
      M.layer = L.tileLayer(tiles.url, { maxZoom: tiles.max_zoom || 19,
                                         referrerPolicy: "strict-origin-when-cross-origin" }).addTo(M.map);
      M.tilesUrl = tiles.url;
    }
    M.map.setView([lat, lon], Math.min(13, tiles.max_zoom || 19));
    if (M.marker) M.marker.setLatLng([lat, lon]);
    else M.marker = L.circleMarker([lat, lon], { radius: 6, weight: 2, interactive: false }).addTo(M.map);
    M.map.invalidateSize();
  }

  /** @brief Show (or hide) the minimap for the file the viewer just opened. */
  async function updateMinimap(rel) {
    const seq = ++M.seq;
    if (!rel || !miniAllowed()) { if (M.box) M.box.classList.add("hidden"); return; }
    let d = null;
    try {
      d = await fetch("/api/map/file?filename=" + encodeURIComponent(rel)).then(r => r.json());
    } catch (e) { d = null; }
    if (seq !== M.seq) return;
    if (!d || !d.success || d.lat == null) {
      M.at = null;
      if (M.box) M.box.classList.add("hidden");
      return;
    }
    const box = miniBox();
    if (!box) return;
    M.at = [d.lat, d.lon, rel];
    box.classList.remove("hidden");
    const pl = box.querySelector("#map_minimap_place");
    pl.textContent = placeText(d.place) || `${(+d.lat).toFixed(4)}, ${(+d.lon).toFixed(4)}`;
    pl.title = d.place ? [placeText(d.place), d.place.continent].filter(Boolean).join(" - ") : "";
    drawMini(d.lat, d.lon, d.tiles || {}).catch(e => console.error(e));
  }

  if (window.registerFileMetaHook) registerFileMetaHook((meta, rel) => { updateMinimap(rel); });

  Object.assign(window, { mapFitAll, mapShowInGallery, mapRescan, mapFilterChanged, mapClearDates, mapOpenAt });
  window.CIMMap = { searchTokens, locationToken, updateMinimap, mapOpenAt };

  /** @brief Register the Map left tab and the Places gallery view. */
  function register() {
    if (window.registerLeftTab)
      registerLeftTab({ id: "map", label: "Map", feature: "tab.map", paneId: "map_pane", onShow });
    if (window.registerGalleryView) registerGalleryView(placesView);
  }
  if (document.readyState === "loading") window.addEventListener("DOMContentLoaded", register);
  else register();
})();