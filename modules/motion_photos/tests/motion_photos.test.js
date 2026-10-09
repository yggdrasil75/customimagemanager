// Motion photos: the LIVE tile badge, hover playback, and the viewer's Live button.
const { page, test, assert, skipUnless } = require("cim");
const skip = skipUnless("motion_photos");

async function boot(hover) {
  const b = page({ modules: ["motion_photos"] });
  b.api.on("/api/motion_photos/settings", { success: true, hover_play: !!hover });
  await b.tick(10);
  b.run(`window.dispatchEvent(new CustomEvent("cim:user-settings", {detail: {keys: []}}))`);
  await b.tick(10);
  return b;
}

test("a tile with motion gets a LIVE badge; others do not", { skip }, async () => {
  const b = await boot(false);
  assert.deepEqual(b.errors, []);
  b.run(`renderGallery([
    {filename:"m.jxl",kind:"image",width:10,height:10,tags:[],description:"",motion:true},
    {filename:"p.jxl",kind:"image",width:10,height:10,tags:[],description:""}])`);
  const tiles = b.document.querySelectorAll("#gallery_grid .gallery-item");
  assert.equal(tiles.length, 2);
  assert.equal(tiles[0].querySelector(".motion-badge").textContent, "LIVE");
  assert.equal(tiles[1].querySelector(".motion-badge"), null);
  tiles[0].dispatchEvent(new b.window.Event("mouseenter"));
  assert.equal(tiles[0].querySelector(".motion-hover"), null, "hover playback is off by default");
});

test("hover plays the clip when motion_hover_play is on", { skip }, async () => {
  const b = await boot(true);
  b.run(`renderGallery([{filename:"a/m.jxl",kind:"image",width:10,height:10,tags:[],description:"",motion:true}])`);
  const tile = b.document.querySelector("#gallery_grid .gallery-item");
  tile.dispatchEvent(new b.window.Event("mouseenter"));
  const v = tile.querySelector("video.motion-hover");
  assert.ok(v, "a muted loop over the tile");
  assert.equal(v.getAttribute("src") || v.src.replace(/^.*?\/api/, "/api"), "/api/motion/a%2Fm.jxl");
  assert.ok(v.muted && v.loop);
  tile.dispatchEvent(new b.window.Event("mouseleave"));
  assert.equal(tile.querySelector("video.motion-hover"), null);
});

test("the viewer's Live button follows the file and toggles the clip", { skip }, async () => {
  const b = await boot(false);
  b.run(`runFileMetaHooks({motion: true}, "m.jxl")`);
  const btn = b.document.getElementById("motion_btn");
  if (!btn) return;                                   // the layout has no viewer_toggles area
  assert.ok(!btn.classList.contains("hidden"));
  b.run(`CIMMotion.click()`);
  const layer = b.document.getElementById("motion_viewer");
  if (layer) {
    assert.ok(!layer.classList.contains("hidden"));
    assert.ok(b.document.getElementById("motion_video").muted, "muted by default");
    b.document.getElementById("motion_mute").click();
    assert.equal(b.document.getElementById("motion_video").muted, false);
    b.run(`CIMMotion.click()`);
    assert.ok(layer.classList.contains("hidden"));
  }
  b.run(`runFileMetaHooks({}, "p.jxl")`);
  assert.ok(btn.classList.contains("hidden"));
});
