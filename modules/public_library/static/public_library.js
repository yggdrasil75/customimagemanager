/* public_library.js - Publish and Arrange now (modules/public_library).
 *
 * "Publish" in the gallery bulk bar (admins) moves the selected private movies, episodes,
 * tracks and books into the public section of the same kind, in the Plex / Jellyfin
 * layout. "Arrange now" under Settings > Media > Libraries re-arranges what is already
 * in the public sections (new files there are arranged on their own). */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const toast = (m) => { if (typeof showToast === "function") showToast(m); else alert(m); };

  async function post(url, body) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                                 body: JSON.stringify(body || {}) });
    return r.json().catch(() => ({ success: false, error: "HTTP " + r.status }));
  }

  /** @brief Publish the gallery selection. */
  async function publishSelected() {
    const files = [...(window.selectedFiles || [])];
    if (!files.length) { toast("Nothing selected."); return; }
    if (!confirm(`Publish ${files.length} file(s) to the public libraries?\n\n` +
                 "They move out of the private folder into movies / tv / music / books, " +
                 "named for Plex and Jellyfin, and every account can see them.")) return;
    const d = await post("/api/public_library/publish", { filenames: files });
    if (!d.success) { toast(d.error || "Publish failed."); return; }
    const sk = (d.skipped || []).length;
    toast(`Published ${d.moved} file(s)` + (d.carried ? `, ${d.carried} beside them` : "") +
          (sk ? `; ${sk} skipped (${d.skipped[0][1]})` : "") + ".");
    if (window.selectedFiles) window.selectedFiles.clear();
    if (typeof loadGallery === "function") loadGallery();
  }

  /** @brief "Arrange now" under the Libraries setting. */
  function mountArrange() {
    const box = $("module_settings_fields_media_libraries");
    if (!box || $("pl_arrange")) return;
    const row = document.createElement("div");
    row.className = "flex items-center gap-2 mt-1 text-xs";
    row.setAttribute("data-feature", "library.publish");
    row.innerHTML = '<button type="button" id="pl_arrange" class="px-2 py-0.5 rounded bg-gray-700 hover:bg-gray-600" ' +
      'title="Rename what is already in the public movies, tv, music and books folders into the Plex / Jellyfin layout">' +
      "Arrange now</button><span id=\"pl_msg\" class=\"text-gray-400\"></span>";
    box.parentNode.appendChild(row);
    $("pl_arrange").addEventListener("click", async () => {
      const d = await post("/api/public_library/arrange", {});
      $("pl_msg").textContent = d.success ? "Arranging in the background..." : (d.error || "failed");
    });
    if (window.CIMFeatures) CIMFeatures.apply(row);
  }

  function register() {
    if (window.registerControlButton) {
      registerControlButton("gallery_bulk", { label: "Publish", onclick: "publicLibraryPublish()",
        feature: "library.publish", variant: "neutral",
        title: "Move the selected private items into the public movies / tv / music / books libraries" });
    }
  }
  window.publicLibraryPublish = publishSelected;
  document.addEventListener("module-settings-tab", () => setTimeout(mountArrange, 0));
  document.addEventListener("click", (ev) => {
    if (ev.target.closest && ev.target.closest('[data-settings-tab="media"]')) setTimeout(mountArrange, 0);
  });
  if (document.readyState === "loading") window.addEventListener("DOMContentLoaded", register);
  else register();
})();