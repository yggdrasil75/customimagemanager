// Frontend tests for modules/panorama: the media mode, the 360 button, the tile badge.
// jsdom has no WebGL (the harness stubs THREE as {}), so the sphere itself is not drawn here.
const { page, test, assert, skipUnless } = require("cim");
const skip = skipUnless("panorama");

test("registers the pano media mode, the 360 button and badges pano tiles", { skip }, async () => {
  const b = page({ modules: ["panorama"] });
  assert.deepEqual(b.errors, []);
  await b.tick(20);                            // DOMContentLoaded: the mode and button register then
  const P = b.window.CIMPanorama;
  assert.ok(P, "window.CIMPanorama is exposed");
  assert.ok(b.window._mediaModes && b.window._mediaModes.pano, "media mode registered");
  assert.ok((b.window._extButtons.viewer_toggles || []).some(h => /pano_btn/.test(h)), "360 button registered");
  const tile = b.document.createElement("div");
  tile.className = "gallery-item";
  for (const h of b.window.galleryTileHooks) h(tile, { filename: "x.jpg", pano: { source: "gpano" } });
  assert.ok(tile.querySelector(".pano-badge"), "pano tile gets a 360 badge");
  const plain = b.document.createElement("div");
  for (const h of b.window.galleryTileHooks) h(plain, { filename: "y.jpg" });
  assert.ok(!plain.querySelector(".pano-badge"));
});

test("open switches the centre to the sphere pane and close returns to the image", { skip }, async () => {
  const b = page({ modules: ["panorama"] });
  await b.tick(20);
  const P = b.window.CIMPanorama;
  if (!b.document.getElementById("pano_viewer")) return;     // pane not in the snapshot page
  b.window.currentFile = "a/p.jpg";
  P.open();
  assert.equal(P.state.open, true);
  assert.equal(P.state.file, "a/p.jpg");
  assert.ok(b.document.getElementById("image_pane").classList.contains("hidden"), "flat viewer hidden");
  assert.match(b.document.getElementById("pano_msg").textContent, /three\.js is not loaded|Loading/);
  P.close();
  assert.equal(P.state.open, false);
  assert.ok(!b.document.getElementById("image_pane").classList.contains("hidden"));
});
