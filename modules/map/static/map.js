/* Map module front-end.
 *
 * Registers the "Map" left tab. Leaflet + MarkerCluster are loaded lazily the
 * first time the tab opens, from /api/map/vendor/ (served out of the pip
 * XStatic packages), so a page that never opens the map never pays for it.
 *
 * Markers are thumbnails; clusters show the newest-in-cluster thumbnail and a
 * count. Clicking a marker opens the file in the editor (selectFile). The open
 * file is highlighted and, if it has a position, centred while the tab shows.
 */
(function () {
  "use strict";

  const VENDOR = "/api/map/vendor/";
  const S = {
    loading: null,       // Promise for the lazy Leaflet load
    map: null,
    cluster: null,
    points: [],          // [[rel, lat, lon, isVideo], …]
    byRel: new Map(),    // rel -> L.Marker
    current: null,       // L.CircleMarker ring on the open file
    currentRel: null,
    fitted: false,
    poll: null,
    scanFinished: 0,     // scan.finished of the set on screen: a new pass => rebuild
  };

  function esc(s) {
    return String(s).replace(/[&<>"']/g, c =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function thumbUrl(rel) { return "/api/thumb/" + encodeURIComponent(rel); }
  function toast(msg) { if (typeof showToast === "function") showToast(msg); }

  function loadCss(href) {
    return new Promise((res, rej) => {
      if (document.querySelector(`link[href="${href}"]`)) return res();
      const l = document.createElement("link");
      l.rel = "stylesheet"; l.href = href;
      l.onload = res; l.onerror = () => rej(new Error("failed to load " + href));
      document.head.appendChild(l);
    });
  }
  function loadJs(src) {
    return new Promise((res, rej) => {
      if (document.querySelector(`script[src="${src}"]`)) return res();
      const s = document.createElement("script");
      s.src = src; s.async = false;
      s.onload = res; s.onerror = () => rej(new Error("failed to load " + src));
      document.head.appendChild(s);
    });
  }
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

  // ── markers ───────────────────────────────────────────────────────────
  function photoIcon(rel, video) {
    return L.divIcon({
      className: "cim-map-pin",
      html: `<div class="cim-map-thumb${video ? " is-video" : ""}">` +
            `<img loading="lazy" src="${esc(thumbUrl(rel))}" alt="">` +
            (video ? '<span class="cim-map-play">▶</span>' : "") + "</div>",
      iconSize: [48, 48],
      iconAnchor: [24, 24],
    });
  }

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

  function openFile(rel) {
    if (typeof selectFile === "function") selectFile(rel);
  }

  function buildMarkers() {
    S.cluster.clearLayers();
    S.byRel.clear();
    const ms = [];
    for (const [rel, lat, lon, video] of S.points) {
      const m = L.marker([lat, lon], { icon: photoIcon(rel, !!video), cimRel: rel, title: rel });
      m.on("click", () => openFile(rel));
      S.byRel.set(rel, m);
      ms.push(m);
    }
    S.cluster.addLayers(ms);
  }

  function updateCount(scan) {
    const el = document.getElementById("map_count");
    if (el) el.textContent = `${S.points.length.toLocaleString()} geotagged`;
    const sc = document.getElementById("map_scan");
    if (sc) sc.textContent = scan && scan.running
      ? `reading locations… ${scan.done.toLocaleString()}/${scan.total.toLocaleString()}` : "";
  }

  async function fetchPoints() {
    const d = await fetch("/api/map/points").then(r => r.json());
    if (!d.success) throw new Error(d.error || "map points failed");
    return d;
  }

  function schedulePoll(scan) {
    clearTimeout(S.poll);
    if (scan && scan.running) S.poll = setTimeout(reload, 4000);
  }

  async function reload() {
    try {
      const d = await fetchPoints();
      const changed = d.points.length !== S.points.length ||
                      (d.scan && d.scan.finished !== S.scanFinished);
      S.scanFinished = d.scan ? d.scan.finished : 0;
      S.points = d.points;
      if (changed) buildMarkers();
      updateCount(d.scan);
      if (changed && !S.fitted) mapFitAll();
      schedulePoll(d.scan);
    } catch (e) { console.error(e); }
  }

  // ── map setup ─────────────────────────────────────────────────────────
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
    buildMarkers();
    updateCount(d.scan);
    schedulePoll(d.scan);
    if (window.ResizeObserver) new ResizeObserver(() => S.map && S.map.invalidateSize()).observe(host);
    if (window.CIMFeatures && !window.CIMFeatures.canWrite("tab.map"))
      document.getElementById("map_rescan_btn")?.classList.add("hidden");
    mapFitAll();
    if (window.currentFile) highlight(window.currentFile, false);
  }

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

  function mapVisible() {
    const pane = document.getElementById("map_pane");
    return !!(S.map && pane && !pane.classList.contains("hidden"));
  }

  // ── open-file highlight ───────────────────────────────────────────────
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

  // ── toolbar actions ───────────────────────────────────────────────────
  function mapFitAll() {
    if (!S.map) return;
    if (!S.points.length) { S.map.setView([20, 0], 2); return; }
    const b = L.latLngBounds(S.points.map(p => [p[1], p[2]]));
    S.map.fitBounds(b.pad(0.1), { maxZoom: 14 });
    S.fitted = true;
  }

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
    const si = document.getElementById("search_input");
    const prev = (si ? si.value : (typeof currentSearch !== "undefined" ? currentSearch : "")) || "";
    const q = (prev.split(/\s+/).filter(t => t && !/^bbox:/i.test(t)).concat(tok)).join(" ");
    if (si) si.value = q;
    if (typeof setPane === "function") setPane("gallery");
    try { currentSearch = q; currentPage = 0; } catch (e) { /* globals.js not loaded */ }
    if (typeof loadGallery === "function") loadGallery();
  }

  async function mapRescan() {
    try {
      const d = await fetch("/api/map/rescan", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ force: true }),
      }).then(r => r.json());
      if (!d.success) { toast(d.error || "Rescan failed"); return; }
      toast("Re-reading locations…");
      updateCount({ running: true, done: 0, total: (d.scan && d.scan.total) || 0 });
      clearTimeout(S.poll);
      S.poll = setTimeout(reload, 1500);
    } catch (e) { toast("Rescan failed: " + e.message); }
  }

  Object.assign(window, { mapFitAll, mapShowInGallery, mapRescan });

  function register() {
    if (window.registerLeftTab)
      registerLeftTab({ id: "map", label: "Map", feature: "tab.map", paneId: "map_pane", onShow });
  }
  if (document.readyState === "loading") window.addEventListener("DOMContentLoaded", register);
  else register();
})();