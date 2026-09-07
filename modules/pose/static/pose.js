/* Pose module front-end.
 *
 * Injected by the module loader (host.add_asset("pose.js")). Owns:
 *   - the skeleton overlay, registered into the core canvas-overlay hook so it
 *     draws on top of the image and vanishes when this module is disabled;
 *   - the 🦴 Pose button (controls panel) + 🗑 Remove skeleton button;
 *   - the bulk 🦴 Pose button in the gallery selection bars.
 * All buttons are injected into named mount points in the core templates; if a
 * mount is absent (template changed), injection is a silent no-op.
 *
 * Reads core viewer state (currentPose, currentFile, selectedFiles) and core
 * helpers (drawCanvas, drawPopout, showToast, selectFile, loadGallery) that
 * remain in the core — this module moved the FEATURE, not the viewer. */
(function () {
  // ── overlay ────────────────────────────────────────────────────────────────
  function drawSkeleton(c, dw, dh, scale) {
    const t = document.getElementById("toggle_skeleton");
    if (!t || !t.checked || !window.currentPose || !currentPose.people) return;
    const edges = currentPose.edges || [];
    c.save();
    c.lineWidth = 2 / (scale || 1);
    currentPose.people.forEach((p) => {
      const kp = p.keypoints || [];
      c.strokeStyle = "#22d3ee";
      edges.forEach((e) => {
        const ka = kp[e[0]], kb = kp[e[1]];
        if (!ka || !kb) return;
        if ((ka.v || 0) < 0.2 || (kb.v || 0) < 0.2) return;
        c.beginPath(); c.moveTo(ka.x * dw, ka.y * dh);
        c.lineTo(kb.x * dw, kb.y * dh); c.stroke();
      });
      c.fillStyle = "#f0abfc";
      kp.forEach((k) => {
        if ((k.v || 0) < 0.2) return;
        c.beginPath(); c.arc(k.x * dw, k.y * dh, 3 / (scale || 1), 0, 7); c.fill();
      });
    });
    c.restore();
  }
  if (window.registerCanvasOverlay) registerCanvasOverlay(drawSkeleton);

  // ── single-image handlers ───────────────────────────────────────────────────
  async function runPose() {
    if (!window.currentFile) { alert("Select an image first."); return; }
    const btn = document.getElementById("btn_pose"); const og = btn.innerText;
    btn.innerText = "🦴 …"; btn.disabled = true;
    try {
      const d = await fetch("/api/pose", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: currentFile }),
      }).then((r) => r.json());
      if (d.success) {
        window.currentPose = d.pose || null;
        const t = document.getElementById("toggle_skeleton"); if (t) t.checked = true;
        syncPoseButtons();
        drawCanvas(); if (typeof popoutOpen !== "undefined" && popoutOpen) drawPopout();
        const n = (d.pose && d.pose.people) ? d.pose.people.length : 0;
        showToast(n ? `Pose: ${n} person(s) detected.` : (d.note || "No people detected."));
      } else alert("Pose failed: " + (d.error || ""));
    } catch (e) { alert("Network error during pose."); }
    btn.innerText = og; btn.disabled = false;
  }

  function syncPoseButtons() {
    const rm = document.getElementById("btn_pose_remove");
    if (rm) rm.style.display =
      (window.currentPose && currentPose.people && currentPose.people.length) ? "block" : "none";
  }

  async function removePose() {
    if (!window.currentFile) { alert("Select an image first."); return; }
    if (!confirm("Delete the stored skeleton for this image? This cannot be undone.")) return;
    const btn = document.getElementById("btn_pose_remove"); const og = btn.innerText;
    btn.innerText = "🗑 …"; btn.disabled = true;
    try {
      const d = await fetch("/api/pose_remove", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: currentFile }),
      }).then((r) => r.json());
      if (d.success) {
        window.currentPose = null;
        const t = document.getElementById("toggle_skeleton"); if (t) t.checked = false;
        syncPoseButtons();
        drawCanvas(); if (typeof popoutOpen !== "undefined" && popoutOpen) drawPopout();
        showToast("Skeleton removed.");
      } else alert("Remove failed: " + (d.error || ""));
    } catch (e) { alert("Network error removing skeleton."); }
    btn.innerText = og; btn.disabled = false;
  }

  // ── bulk handler ────────────────────────────────────────────────────────────
  async function bulkPose() {
    const files = [...(window.selectedFiles || [])];
    if (!files.length) return;
    const btn = document.querySelector('.pose-bulk-btn');
    const orig = btn ? btn.innerHTML : ""; if (btn) { btn.disabled = true; btn.innerHTML = "🦴 …"; }
    showToast(`Estimating pose on ${files.length} image(s)…`);
    try {
      const d = await fetch("/api/bulk_pose", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filenames: files }),
      }).then((r) => r.json());
      if (!d.success) { alert("Pose failed: " + (d.error || "")); }
      else {
        showToast(`Pose: ${d.posed}/${d.done} had people${d.errors.length ? ", " + d.errors.length + " errors" : ""}.`);
        if (window.currentFile && files.includes(currentFile)) selectFile(currentFile);
        loadGallery(); refreshReviewCount();
      }
    } catch (e) { alert("Network error during pose estimation."); }
    finally { document.querySelectorAll(".pose-bulk-btn").forEach((b) => { b.disabled = false; b.innerHTML = orig; }); }
  }

  // expose the ones the core still calls by name (gallery.js guards on typeof)
  window.runPose = runPose;
  window.removePose = removePose;
  window.bulkPose = bulkPose;
  window.syncPoseButtons = syncPoseButtons;

  // ── button injection ────────────────────────────────────────────────────────
  // Append buttons to the general AI-tools and gallery-bulk extension areas.
  // No pose-specific mount points in the core template — any module can do this.
  function buildButtons() {
    if (!window.registerControlButton) return;
    registerControlButton("ai_tools",
      '<button onclick="runPose()" id="btn_pose" data-feature="ai.pose" ' +
      'class="w-full bg-cyan-700 hover:bg-cyan-600 py-1.5 rounded font-bold text-sm">🦴 Pose</button>');
    registerControlButton("ai_tools",
      '<button onclick="removePose()" id="btn_pose_remove" style="display:none" ' +
      'data-feature="ai.pose_remove" title="Delete the current (bad) skeleton from this image" ' +
      'class="w-full bg-rose-800 hover:bg-rose-700 py-1.5 rounded font-bold text-sm">🗑 Remove skeleton</button>');
    registerControlButton("gallery_bulk",
      '<button onclick="bulkPose()" data-feature="ai.pose" ' +
      'title="Estimate a skeleton/pose on every selected image and store it — this is what ' +
      'T-pose aggregation reads, so run it over a person\'s images before Estimate T-pose" ' +
      'class="pose-bulk-btn text-xs bg-cyan-700 hover:bg-cyan-600 px-3 py-1.5 rounded font-bold">🦴 Pose</button>');
  }

  if (document.readyState === "loading")
    window.addEventListener("DOMContentLoaded", buildButtons);
  else buildButtons();
})();
