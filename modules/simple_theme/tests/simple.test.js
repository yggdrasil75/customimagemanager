// Simple / Intermediate layouts on top of the core theming + viewer.
const { page, test, assert, skipUnless, hasModule } = require("cim");
const skip = skipUnless("simple_theme");

const META = { success: true, metadata: { tags: ["beach", "?sunset"], description: "Grandma at the beach",
  regions: [
    { class_name: "Jill", region_name: "Jill", region_type: "face", cx: .5, cy: .5, w: .1, h: .1, confirmed: true },
    { class_name: "face", region_type: "face", cx: .2, cy: .2, w: .1, h: .1, confirmed: false },
    { class_name: "dog", region_type: "dog", cx: .8, cy: .8, w: .1, h: .1, confirmed: true },
    { class_name: "Jill", region_name: "Jill", region_type: "person", cx: .5, cy: .6, w: .3, h: .6, confirmed: true },
  ], albums: [] } };

function themes(layout) {
  return { success: true, can_choose: true, defaults: { layout: "advanced", palette: "blue" },
    themes: { layout: [{ id: "advanced", label: "A" }, { id: "simple", label: "S" }, { id: "intermediate", label: "I" }], palette: [] },
    selected: { layout, palette: "" } };
}
const MODS = ["theming", "timeline", "simple_theme", "intermediate_theme", "advanced_theme"].filter(hasModule);

async function boot(layout) {
  const b = page({ modules: MODS });
  let cur = layout;
  b.api.on("/api/theme", () => themes(cur));
  b.api.on("POST /api/user/settings", c => { if (c.body.layout) cur = c.body.layout; return { success: true, fields: [] }; });
  b.api.on("POST /api/metadata", c => c.body.action === "read" ? META : { success: true });
  b.api.on("POST /api/albums/of", { success: true, albums: ["Holiday"], all: ["Holiday"] });
  b.api.on("/api/albums", { success: true, albums: [{ name: "Holiday", count: 3, cover: "h.jxl" }, { name: "Pets", count: 0, cover: "" }] });
  await b.tick(40);
  return b;
}

test("simple: body attribute set, viewer active, timeline forced, albums strip", { skip }, async () => {
  const b = await boot("simple");
  assert.deepEqual(b.errors, []);
  assert.equal(b.document.body.dataset.layout, "simple");
  assert.equal(b.run("CIMSimpleViewer.active"), "simple");
  if (hasModule("timeline")) assert.equal(b.run("galleryView"), "timeline");
  const cards = b.document.querySelectorAll("#sv_albums_strip .sv-card");
  assert.equal(cards.length, 2);
  cards[0].click();
  await b.tick(10);
  assert.equal(b.run("currentAlbum"), "Holiday");
  assert.ok(b.document.querySelector('#sv_albums_strip .sv-card[data-album="Holiday"]').classList.contains("on"));
});

test("simple: selecting a file opens the viewer with people, position, meta and arrow nav", { skip }, async () => {
  const b = await boot("simple");
  b.run(`galleryFiles=[{filename:"a.jxl"},{filename:"b.jxl"},{filename:"c.jxl"}]`);
  await b.run(`selectFile("b.jxl")`);
  await b.tick(10);
  assert.ok(!b.document.getElementById("popout_modal").classList.contains("hidden"));
  assert.ok(b.document.getElementById("popout_modal").classList.contains("sv-active"));
  assert.deepEqual([...b.document.querySelectorAll("#sv_people .sv-person")].map(e => e.textContent), ["Jill", "Unknown person"]);
  assert.equal(b.document.getElementById("sv_pos").textContent, "2 / 3");
  assert.equal(b.run("popoutBoxesEditable()"), false);
  b.run(`CIMSimpleViewer.toggleMeta()`);
  assert.ok(!b.document.getElementById("sv_meta_simple").classList.contains("hidden"));
  assert.equal(b.document.getElementById("sv_desc").textContent, "Grandma at the beach");
  assert.deepEqual([...b.document.querySelectorAll("#sv_tags .sv-tag")].map(e => e.textContent), ["beach", "sunset"]);
  assert.deepEqual([...b.document.querySelectorAll("#sv_albums .sv-tag")].map(e => e.textContent), ["Holiday"]);
  b.document.dispatchEvent(new b.window.KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
  await b.tick(10);
  assert.equal(b.run("window.currentFile"), "c.jxl");
  assert.equal(b.document.getElementById("sv_arrow_next").disabled, true);
  b.run(`CIMSimpleViewer.step(1)`);
  assert.equal(b.run("window.currentFile"), "c.jxl");
});

test("simple: panels respect feature gates (tags denied -> hidden)", { skip }, async () => {
  const b = await boot("simple");
  b.run(`CIMAuth.user.features = Object.assign({}, CIMAuth.user.features, {"annot.tags": 0});`);
  b.run(`galleryFiles=[{filename:"a.jxl"}]`);
  await b.run(`selectFile("a.jxl")`);
  await b.tick(10);
  b.run(`CIMSimpleViewer.toggleMeta()`);
  assert.ok(b.document.querySelector('#sv_meta_simple [data-feature="annot.tags"]').classList.contains("cim-feature-hidden"));
  assert.ok(!b.document.querySelector('#sv_meta_simple [data-feature="annot.description"]').classList.contains("cim-feature-hidden"));
});

test("intermediate: Meta moves the controls pane in and back; off-limits tabs bounce", { skip: skip || skipUnless("intermediate_theme") }, async () => {
  const b = await boot("intermediate");
  assert.equal(b.run("CIMSimpleViewer.active"), "intermediate");
  b.run(`galleryFiles=[{filename:"a.jxl"}]`);
  await b.run(`selectFile("a.jxl")`);
  await b.tick(10);
  assert.ok(!b.document.getElementById("popout_modal").classList.contains("hidden"));
  b.run(`CIMSimpleViewer.toggleMeta()`);
  assert.equal(b.document.getElementById("controls_pane").parentElement.id, "sv_meta_host");
  assert.ok(b.document.getElementById("sv_meta_simple").classList.contains("hidden"));
  b.run(`closePopout()`);
  assert.equal(b.document.getElementById("controls_pane").parentElement.id, "editor_region");
  b.run(`setPane('review')`);
  await b.run(`CIMTheme.set("layout", "intermediate")`);
  assert.equal(b.run("currentPane"), "gallery");
});

test("advanced: viewer released, a click does not open the popout", { skip }, async () => {
  const b = await boot("advanced");
  assert.equal(b.run("CIMSimpleViewer.active"), null);
  await b.run(`selectFile("a.jxl")`);
  await b.tick(10);
  assert.ok(b.document.getElementById("popout_modal").classList.contains("hidden"));
});

test("switching simple -> advanced closes the viewer and restores", { skip }, async () => {
  const b = await boot("simple");
  b.run(`galleryFiles=[{filename:"a.jxl"}]`);
  await b.run(`selectFile("a.jxl")`);
  await b.tick(10);
  assert.ok(!b.document.getElementById("popout_modal").classList.contains("hidden"));
  await b.run(`CIMTheme.set("layout", "advanced")`);
  assert.equal(b.run("CIMSimpleViewer.active"), null);
  assert.ok(b.document.getElementById("popout_modal").classList.contains("hidden"));
  assert.equal(b.document.body.dataset.layout, "advanced");
});