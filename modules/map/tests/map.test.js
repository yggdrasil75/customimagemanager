// Map: the Places gallery view, location: tokens built from it, and the viewer minimap.
const { page, test, assert, skipUnless } = require("cim");
const skip = skipUnless("map");

const TREE = { success: true, available: true, total: 3, countries: [
  { cc: "US", name: "United States", continent: "North America", count: 2, regions: [
    { name: "North Carolina", count: 2, cities: [{ name: "Raleigh", count: 2 }] }] },
  { cc: "JP", name: "Japan", continent: "Asia", count: 1, regions: [
    { name: "Tokyo", count: 1, cities: [{ name: "Tokyo", count: 1 }] }] }] };

async function boot() {
  const b = page({ modules: ["map"] });
  b.api.on("/api/map/places", TREE);
  await b.tick(10);
  return b;
}

test("search tokens keep a quoted value whole", { skip }, async () => {
  const b = await boot();
  assert.deepEqual(b.errors, []);
  const toks = b.val(`CIMMap.searchTokens('cat location:"north carolina" date:2020')`);
  assert.deepEqual(toks, ["cat", 'location:"north carolina"', "date:2020"]);
  assert.equal(b.run(`CIMMap.locationToken(["Raleigh", "North Carolina", "US"])`),
               'location:"Raleigh / North Carolina / US"');
});

test("the Places view lists countries, regions and cities; a click searches location:", { skip }, async () => {
  const b = await boot();
  b.run(`setGalleryView("places")`);
  await b.tick(20);
  const host = b.document.getElementById("gallery_view_host");
  assert.ok(b.api.last("/api/map/places"));
  const names = [...host.querySelectorAll(".map-place-country-name")].map(e => e.textContent);
  assert.deepEqual(names, ["United States", "Japan"]);
  b.run(`document.getElementById("search_input").value = 'cat location:"old place"'`);
  host.querySelector('.map-place-city[data-loc*="Raleigh"]').click();
  await b.tick(20);
  const q = b.document.getElementById("search_input").value;
  assert.equal(q, 'cat location:"Raleigh / North Carolina / US"');
  assert.equal(b.run("currentSearch"), q);
});

test("the minimap shows for a geotagged file and hides for one without GPS", { skip }, async () => {
  const b = await boot();
  b.api.on("/api/map/file", c => (c.query.filename === "geo.jxl"
    ? { success: true, filename: "geo.jxl", lat: 35.77, lon: -78.63,
        place: { city: "Raleigh", admin1: "North Carolina", country: "United States", continent: "North America" },
        tiles: { url: "https://t/{z}/{x}/{y}.png", max_zoom: 19 } }
    : { success: true, filename: c.query.filename, lat: null, lon: null }));
  b.run(`CIMMap.updateMinimap("geo.jxl")`);
  await b.tick(20);
  const box = b.document.getElementById("map_minimap_box");
  assert.ok(box && !box.classList.contains("hidden"));
  assert.ok(b.document.getElementById("editor_panel").contains(box));
  assert.equal(b.document.getElementById("map_minimap_place").textContent,
               "Raleigh, North Carolina, United States");
  b.run(`CIMMap.updateMinimap("plain.jxl")`);
  await b.tick(20);
  assert.ok(box.classList.contains("hidden"));
});
