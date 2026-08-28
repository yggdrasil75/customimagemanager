/* Person mesh viewer.
 *
 * Owns the centre-pane #person_pane: a Three.js scene showing one appearance's
 * canonical body mesh (or its T-pose skeleton when no mesh exists yet), plus a
 * scrub bar that steps through the person's appearances in time order. Dragging
 * the scrub bar swaps which appearance is rendered, so you can drag across a
 * life and watch the body shape change.
 *
 * openPerson() (faces.js) drives this: it loads the person record, fills the
 * right-pane "Person" tab, then calls personView.open(cid, person) to take over
 * the centre pane.
 */
(function () {
  "use strict";

  let scene, camera, renderer, controls, current; // current mesh/skeleton group
  let raf = null;
  let state = { cid: null, eras: [], idx: 0, view: "body" };

  function container() { return document.getElementById("person_mesh_container"); }

  // Lazily build the renderer the first time we show a person. Three is loaded
  // globally (static/vendor/three.min.js); guard so a missing bundle degrades
  // to the "no mesh" placeholder rather than throwing.
  function ensureScene() {
    if (renderer) return true;
    if (typeof THREE === "undefined") return false;
    const el = container();
    if (!el) return false;

    scene = new THREE.Scene();
    scene.background = new THREE.Color(0x000000);

    const w = el.clientWidth || 400, h = el.clientHeight || 400;
    camera = new THREE.PerspectiveCamera(45, w / h, 0.01, 100);
    camera.position.set(0, 1.1, 3.2);

    renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(window.devicePixelRatio || 1);
    renderer.setSize(w, h);
    el.appendChild(renderer.domElement);

    scene.add(new THREE.HemisphereLight(0xffffff, 0x223344, 1.0));
    const key = new THREE.DirectionalLight(0xffffff, 0.8);
    key.position.set(2, 4, 3);
    scene.add(key);

    const grid = new THREE.GridHelper(4, 8, 0x334155, 0x1e293b);
    scene.add(grid);

    if (THREE.OrbitControls) {
      controls = new THREE.OrbitControls(camera, renderer.domElement);
      controls.target.set(0, 1, 0);
      controls.enableDamping = true;
      controls.update();
    }

    window.addEventListener("resize", resize);
    animate();
    return true;
  }

  function resize() {
    if (!renderer) return;
    const el = container();
    if (!el || el.offsetParent === null) return; // hidden
    const w = el.clientWidth, h = el.clientHeight;
    if (!w || !h) return;
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h);
  }

  function animate() {
    raf = requestAnimationFrame(animate);
    if (controls) controls.update();
    if (renderer) renderer.render(scene, camera);
  }

  function clearCurrent() {
    if (current) { scene.remove(current); current = null; }
  }

  // Frame the camera/orbit target on a freshly added object's bounds so meshes
  // of any scale (metres, arbitrary units) sit centred and fully in view.
  function frame(obj) {
    const box = new THREE.Box3().setFromObject(obj);
    if (box.isEmpty()) return;
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());
    const maxDim = Math.max(size.x, size.y, size.z) || 1;
    obj.position.sub(center);            // recentre on origin
    obj.position.y += size.y / 2;        // stand on the grid
    const dist = maxDim * 2.2;
    camera.position.set(0, size.y * 0.6, dist);
    if (controls) { controls.target.set(0, size.y / 2, 0); controls.update(); }
    else camera.lookAt(0, size.y / 2, 0);
  }

  function setEmpty(on) {
    const e = document.getElementById("person_mesh_empty");
    if (e) e.classList.toggle("hidden", !on);
  }

  // Build a simple line skeleton from stored T-pose keypoints as a fallback when
  // no mesh has been estimated. Points are {name:[x,y,z]} or an array of joints;
  // we just connect what edges we can and drop a small sphere at each joint.
  function buildSkeleton(tpose) {
    const g = new THREE.Group();
    let pts = tpose && (tpose.keypoints || tpose.points || tpose.joints || tpose);
    if (!pts) return null;
    // Normalise to a name->[x,y,z] map.
    let named = {};
    if (Array.isArray(pts)) pts.forEach((p, i) => { named[p.name || i] = p.xyz || p.position || p; });
    else named = pts;

    const mat = new THREE.MeshBasicMaterial({ color: 0x38bdf8 });
    const sph = new THREE.SphereGeometry(0.02, 8, 8);
    const vecs = {};
    let any = false;
    for (const k in named) {
      const v = named[k];
      if (!v || v.length < 3 || (v[0] == null)) continue;
      const p = new THREE.Vector3(v[0], v[1], v[2]);
      vecs[k] = p;
      const m = new THREE.Mesh(sph, mat);
      m.position.copy(p);
      g.add(m);
      any = true;
    }
    if (!any) return null;

    // Draw bones for any provided edge list (tpose.skeleton = [[a,b],…]).
    const edges = tpose.skeleton || tpose.edges;
    if (Array.isArray(edges)) {
      const lmat = new THREE.LineBasicMaterial({ color: 0x0ea5e9 });
      edges.forEach(([a, b]) => {
        const pa = vecs[a], pb = vecs[b];
        if (pa && pb) {
          const geo = new THREE.BufferGeometry().setFromPoints([pa, pb]);
          g.add(new THREE.Line(geo, lmat));
        }
      });
    }
    return g;
  }

  async function render(i) {
    const era = state.eras[i];
    if (!era) return;
    state.idx = i;
    updateScrubLabel();
    if (!ensureScene()) { setEmpty(true); return; }
    clearCurrent();
    setEmpty(false);

    const base = "/api/persons/" + state.cid;

    if (state.view === "face") {
      if (era.has_face_mesh && THREE.OBJLoader) {
        try {
          const txt = await (await fetch(base + "/face_mesh_data/" + encodeURIComponent(era.id))).text();
          const obj = new THREE.OBJLoader().parse(txt);
          obj.traverse(c => {
            if (c.isMesh) c.material = new THREE.MeshStandardMaterial(
              { color: 0xd8c2a8, roughness: 0.7, metalness: 0.0, flatShading: false });
          });
          clearCurrent();
          current = obj; scene.add(obj); frame(obj);
          return;
        } catch (e) { /* fall through to placeholder */ }
      }
      setEmpty(true);
      return;
    }

    // Body view. Mesh first.
    if (era.has_mesh && THREE.OBJLoader) {
      try {
        const txt = await (await fetch(base + "/mesh_data/" + encodeURIComponent(era.id))).text();
        const obj = new THREE.OBJLoader().parse(txt);
        obj.traverse(c => {
          if (c.isMesh) c.material = new THREE.MeshStandardMaterial(
            { color: 0xb0b4bb, roughness: 0.8, metalness: 0.0, flatShading: false });
        });
        clearCurrent();
        current = obj; scene.add(obj); frame(obj);
        return;
      } catch (e) { /* fall through to skeleton */ }
    }
    // T-pose skeleton fallback.
    if (era.has_tpose) {
      try {
        const tp = await (await fetch(base + "/tpose_data/" + encodeURIComponent(era.id))).json();
        const sk = buildSkeleton(tp);
        if (sk) { clearCurrent(); current = sk; scene.add(sk); frame(sk); return; }
      } catch (e) { /* fall through */ }
    }
    setEmpty(true);
  }

  // Reflect the active view in the toggle buttons and the empty-state hint.
  function syncToggle() {
    const wrap = document.getElementById("person_view_toggle");
    if (wrap) wrap.classList.remove("hidden");
    const f = document.getElementById("person_view_face");
    const b = document.getElementById("person_view_body");
    const on = "bg-blue-600 text-white", off = "bg-gray-700 text-gray-300";
    if (f) f.className = "text-[11px] px-2 py-0.5 " + (state.view === "face" ? on : off);
    if (b) b.className = "text-[11px] px-2 py-0.5 " + (state.view === "body" ? on : off);
    const e = document.getElementById("person_mesh_empty");
    if (e) e.textContent = state.view === "face"
      ? "No face mesh for this appearance yet — estimate one in the Person tab."
      : "No mesh or T-pose for this appearance yet — estimate one in the Person tab.";
  }

  function setView(v) {
    v = (v === "face") ? "face" : "body";
    if (v === state.view) return;
    state.view = v;
    syncToggle();
    render(state.idx);
  }

  function eraLabel(era) {
    if (era.label) return era.label;
    const s = era.date_span || {};
    if (s.min || s.max) return (s.min || "?") + " – " + (s.max || "?");
    return era.id;
  }

  function updateScrubLabel() {
    const era = state.eras[state.idx];
    const lbl = document.getElementById("person_scrub_label");
    const hdr = document.getElementById("person_pane_era");
    const text = era ? (eraLabel(era) + "  (" + (state.idx + 1) + "/" + state.eras.length + ")") : "";
    if (lbl) lbl.textContent = era ? (era.rel_paths ? era.rel_paths.length + " photo(s)" : "") : "";
    if (hdr) hdr.textContent = text;
    // Highlight the active dot and show only its label (labels would collide when
    // there are many eras, so the active one is the only text shown on the bar).
    document.querySelectorAll("#person_scrub_ticks .tick-dot").forEach((t, i) => {
      const on = i === state.idx;
      t.classList.toggle("bg-blue-400", on);
      t.classList.toggle("bg-gray-600", !on);
    });
    document.querySelectorAll("#person_scrub_ticks .tick-lab").forEach((t, i) => {
      t.classList.toggle("hidden", i !== state.idx);
    });
  }

  function buildTicks() {
    const wrap = document.getElementById("person_scrub_ticks");
    if (!wrap) return;
    const n = state.eras.length;
    // Each era is a dot positioned at the same fraction i/(n-1) as the range
    // thumb, so dots and slider stay in lockstep. Only the active era's text
    // label is shown (below the dot); the rest are dots to avoid overlap.
    wrap.innerHTML = state.eras.map((e, i) => {
      const pct = n > 1 ? (i / (n - 1)) * 100 : 50;
      const lbl = (eraLabel(e) || "").replace(/"/g, "&quot;");
      return `<span onclick="onPersonScrub(${i})"
          style="position:absolute;left:${pct}%;transform:translateX(-50%)"
          class="cursor-pointer" title="${lbl}">
          <span class="tick-dot block w-1.5 h-1.5 rounded-full bg-gray-600 hover:bg-blue-300 mx-auto"></span>
          <span class="tick-lab hidden absolute left-1/2 -translate-x-1/2 top-2 whitespace-nowrap text-blue-400">${eraLabel(e)}</span>
        </span>`;
    }).join("");
  }

  // Public: take over the centre pane for this person.
  function open(cid, person) {
    // Appearances are built in chronological rank order on the server and given
    // ids "era0", "era1", … where the number IS the time rank (0 = earliest).
    // Sort by that number so scrubbing moves through time. A plain string sort
    // would order "era12" before "era5" (lexicographic), and date_span is often
    // empty (eras form from embedding drift, not dates), so neither is reliable
    // — the rank in the id is the authoritative key.
    const rank = e => {
      const m = /(\d+)/.exec(e.id || "");
      if (m) return parseInt(m[1], 10);
      const dm = e.date_span && e.date_span.min;
      return (typeof dm === "number") ? dm : Number.MAX_SAFE_INTEGER;
    };
    const eras = (person.appearances || []).slice().sort((a, b) => rank(a) - rank(b));
    const prevView = (state && state.view) || "body";
    state = { cid, eras, idx: 0, view: prevView };
    syncToggle();

    const nameEl = document.getElementById("person_pane_name");
    if (nameEl) nameEl.textContent = person.name || ("Person #" + cid);

    const scrub = document.getElementById("person_scrub");
    if (scrub) {
      scrub.min = 0;
      scrub.max = Math.max(0, eras.length - 1);
      scrub.value = 0;
      scrub.disabled = eras.length <= 1;
    }
    buildTicks();

    setMediaMode("person");
    // Defer first render until the pane is visible so the canvas sizes correctly.
    setTimeout(() => { resize(); if (eras.length) render(0); else { ensureScene(); setEmpty(true); updateScrubLabel(); } }, 30);
  }

  function scrubTo(i) {
    i = Math.max(0, Math.min(state.eras.length - 1, parseInt(i, 10) || 0));
    const scrub = document.getElementById("person_scrub");
    if (scrub) scrub.value = i;
    render(i);
  }

  function close() {
    const wrap = document.getElementById("person_view_toggle");
    if (wrap) wrap.classList.add("hidden");
    setMediaMode("image");
  }

  window.personView = { open, close, setView };
  window.onPersonScrub = scrubTo;
  window.closePersonView = close;
  window.personSetView = setView;
})();