// Metadata front end: the "Date taken" row in the editor pane and the Tags gallery view.
const { page, test, assert, skipUnless } = require("cim");
const skip = skipUnless("metadata");

const DATE = { success: true, filename: "a.jxl", datetime: "2019-07-04T15:30:00", offset: "+02:00",
  source: "DateTimeOriginal", fields_present: ["DateTimeOriginal"], buckets: { d_original: "2019-07-04" },
  fields: ["DateTimeOriginal", "CreateDate", "photoshop:DateCreated", "exif:DateTimeOriginal", "xmp:CreateDate"],
  default_fields: ["DateTimeOriginal", "photoshop:DateCreated", "exif:DateTimeOriginal"] };

const TREE = { success: true, files: 3, tree: [
  { name: "places", path: "places", count: 3, children: [
    { name: "usa", path: "places/usa", count: 2, children: [
      { name: "nc", path: "places/usa/nc", count: 1, children: [] }] }] },
  { name: "beach", path: "beach", count: 1, children: [] }] };

test("the Date taken row shows the file's date and saves an edit", { skip }, async () => {
  const b = page({ modules: ["metadata"] });
  assert.deepEqual(b.errors, []);
  b.api.on("/api/metadata/date", DATE);
  b.api.on("POST /api/metadata/date", { success: true, datetime: "2019-07-04T18:30:00", offset: "+05:00" });
  await b.tick(10);
  const row = b.document.getElementById("meta_date_row");
  assert.ok(row, "row inserted under the description");
  b.run(`runFileMetaHooks({}, "a.jxl")`);
  await b.tick(20);
  assert.match(b.document.getElementById("meta_date_value").textContent, /2019-07-04 15:30:00\s+\+02:00/);
  b.run("metaDateEdit()");
  assert.equal(b.document.getElementById("meta_date_d").value, "2019-07-04");
  assert.equal(b.document.getElementById("meta_date_tz").value, "+02:00");
  b.document.getElementById("meta_date_tz").value = "+05:00";
  b.document.getElementById("meta_date_instant").checked = true;
  b.run("metaDateSave()");
  await b.tick(20);
  const sent = b.api.last("/api/metadata/date", "POST");
  assert.ok(sent, "POST /api/metadata/date");
  const body = sent.body;
  assert.equal(body.offset, "+05:00");
  assert.equal(body.tz_mode, "keep_instant");
  assert.equal(body.from_offset, "+02:00");
  assert.deepEqual(body.fields, DATE.default_fields);
});

test("the Tags view lists the tree and a click searches tagpath:", { skip }, async () => {
  const b = page({ modules: ["metadata"] });
  b.api.on("/api/tags/tree", TREE);
  await b.tick(10);
  b.run(`setGalleryView("tags")`);
  await b.tick(20);
  const host = b.document.getElementById("gallery_view_host");
  assert.ok(b.api.last("/api/tags/tree"));
  const names = [...host.querySelectorAll(".tb-root > .tb-node > .tb-row .tb-name")].map(e => e.textContent);
  assert.deepEqual(names, ["places"], "flat tags sit in the Untagged hierarchy group");
  host.querySelector('[data-tb-toggle="places"]').click();
  await b.tick(5);
  host.querySelector('[data-tb-open="places/usa"]').click();
  await b.tick(20);
  assert.equal(b.document.getElementById("search_input").value, "tagpath:places/usa");
  assert.equal(b.run("currentSearch"), "tagpath:places/usa");
});
