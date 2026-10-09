// Intermediate layout: the people in the open picture as avatar chips that search
// person:<id> on click, with "change person" through the People module's picker.
const { page, test, assert, skipUnless, hasModule } = require("cim");
const skip = skipUnless("intermediate_theme") || skipUnless("simple_theme") || skipUnless("people");

const META = { success: true, metadata: { tags: [], description: "", albums: [], regions: [
  { class_name: "face", region_name: "Jill", region_type: "face", cx: .5, cy: .5, w: .1, h: .1, confirmed: true },
  { class_name: "face", region_name: "", region_type: "face", cx: .2, cy: .2, w: .1, h: .1, confirmed: false },
] } };
const THEME = { success: true, can_choose: true, defaults: { layout: "intermediate", palette: "blue" },
  themes: { layout: [{ id: "advanced", label: "A" }, { id: "intermediate", label: "I" }], palette: [] },
  selected: { layout: "intermediate", palette: "" } };
const MODS = ["theming", "timeline", "simple_theme", "intermediate_theme", "faces", "people"].filter(hasModule);

async function boot() {
  const b = page({ modules: MODS });
  b.api.on("/api/theme", THEME);
  b.api.on("POST /api/metadata", c => c.body.action === "read" ? META : { success: true });
  b.api.on("/api/faces/in_file", { success: true, faces: [
    { id: 7, cx: .5, cy: .5, w: .1, h: .1, cluster_id: 42, name: "Jill", uuid: "u-jill", favorite: false, hidden: false },
    { id: 8, cx: .2, cy: .2, w: .1, h: .1, cluster_id: -1, name: "", uuid: null, favorite: false, hidden: false }] });
  b.api.on("/api/persons/directory", { success: true, people: [
    { uuid: "u-ann", name: "Ann", cluster_id: 44, favorite: true },
    { uuid: "u-bob", name: "Bob", cluster_id: 43, favorite: false }] });
  await b.tick(40);
  b.run(`galleryFiles=[{filename:"a.jxl"}]`);
  await b.run(`selectFile("a.jxl")`);
  await b.tick(20);
  return b;
}

test("intermediate: people chips get avatars and search person:<id>", { skip }, async () => {
  const b = await boot();
  assert.deepEqual(b.errors, []);
  const chips = [...b.document.querySelectorAll("#sv_people .sv-person")];
  assert.equal(chips.length, 2);
  assert.ok(chips.every(c => c.querySelector(".iv-avatar")), "every chip has an avatar");
  assert.match(chips[0].querySelector(".iv-avatar").style.backgroundImage, /\/api\/thumb\/a\.jxl/);
  assert.equal(chips[0].dataset.cluster, "42");
  assert.equal(chips[1].dataset.cluster, undefined, "an unclustered face is not a link");
  chips[0].click();
  assert.equal(b.document.getElementById("search_input").value, "person:42");
});

test("intermediate: change person on a chip assigns through the picker", { skip }, async () => {
  const b = await boot();
  b.api.on("POST /api/faces/assign", { success: true, name: "Bob", cluster_id: 43, face_id: 7 });
  b.document.querySelector("#sv_people .sv-person .iv-change").click();
  await b.tick(20);
  const rows = [...b.document.querySelectorAll("#cim_pp_list [data-i]")].map(e => e.textContent.trim());
  assert.match(rows[0], /Ann/, "favourites first");
  b.document.querySelectorAll("#cim_pp_list [data-i]")[1].click();
  await b.tick(20);
  const call = b.api.last("/api/faces/assign", "POST");
  assert.equal(call.body.filename, "a.jxl");
  assert.equal(call.body.person_id, "u-bob");
  assert.equal(call.body.region.cx, .5);
  assert.equal(b.run("currentRegions[0].region_name"), "Bob");
  assert.equal(b.document.getElementById("cim_people_picker"), null);
});

test("viewer region list: a face row has Change person; Remove from person unassigns", { skip }, async () => {
  const b = await boot();
  b.api.on("POST /api/faces/unassign", { success: true, face_id: 7 });
  b.run("renderRegionsList()");
  const btns = b.document.querySelectorAll("#regions_list .region-person");
  assert.equal(btns.length, 2);
  btns[0].click();
  await b.tick(10);
  b.document.getElementById("cim_pp_remove").click();
  await b.tick(20);
  assert.equal(b.api.last("/api/faces/unassign", "POST").body.region.cx, .5);
  assert.equal(b.run("currentRegions[0].region_name"), "");
});
