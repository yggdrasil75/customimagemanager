const { page, test, assert } = require("cim");
const b = page();
const $ = id => b.document.getElementById(id);
const box = (o = {}) => Object.assign({ class_name: "person", cx: .5, cy: .5, w: .4, h: .8, confirmed: false, region_tags: [], region_description: "" }, o);

test("saveRegion on a new box: type = class, name blank, confirmed", () => {
  b.run(`window.currentFile="x.jxl"; currentRegions=[]; editingBoxIdx=null;
         pendingBox={cx:.5,cy:.5,w:.2,h:.2};
         document.getElementById("modal_region_name").value=" girl ";
         saveRegion();`);
  const r = b.val("currentRegions")[0];
  assert.equal(r.class_name, "girl");
  assert.equal(r.region_type, "girl");
  assert.equal(r.region_name, "");
  assert.equal(r.confirmed, true);
  assert.equal(r.uuid, null);
  assert.ok($("region_modal").classList.contains("hidden"));
});

test("saveRegion with empty name falls back to 'region'", () => {
  b.run(`pendingBox={cx:.1,cy:.1,w:.1,h:.1}; document.getElementById("modal_region_name").value=""; saveRegion();`);
  assert.equal(b.val("currentRegions").at(-1).class_name, "region");
});

test("renaming an existing box drags type along unless the type was overridden", () => {
  b.run(`currentRegions=[${JSON.stringify(box({ region_type: "person" }))}, ${JSON.stringify(box({ region_type: "Face", class_name: "face" }))}];
         editingBoxIdx=0; document.getElementById("modal_region_name").value="boy"; saveRegion();
         editingBoxIdx=1; document.getElementById("modal_region_name").value="jill_face"; saveRegion();`);
  const [a, c] = b.val("currentRegions");
  assert.equal(a.class_name, "boy"); assert.equal(a.region_type, "boy");
  assert.equal(c.class_name, "jill_face"); assert.equal(c.region_type, "Face");
});

test("confirmRegion / renameRegion / deleteRegion", () => {
  b.run(`currentRegions=[${JSON.stringify(box())}, ${JSON.stringify(box({ class_name: "face" }))}]; selectedRegionIdx=1;
         confirmRegion(0); renameRegion(1, " cat "); `);
  let r = b.val("currentRegions");
  assert.equal(r[0].confirmed, true); assert.equal(r[1].class_name, "cat");
  b.run(`deleteRegion(0)`);
  r = b.val("currentRegions");
  assert.equal(r.length, 1); assert.equal(r[0].class_name, "cat");
  assert.equal(b.run("selectedRegionIdx"), 0);           // shifted down with the splice
  b.run(`deleteRegion(0)`);
  assert.equal(b.run("selectedRegionIdx"), -1);
});

test("region editor shows instance name and type (type defaults to class)", () => {
  b.run(`currentRegions=[${JSON.stringify(box({ region_name: "jill" }))}]; selectRegion(0);`);
  assert.equal($("region_name").value, "jill");
  assert.equal($("region_type").value, "person");
  b.run(`document.getElementById("region_type").value="Full body"; onRegionTypeInput();
         document.getElementById("region_name").value="jane"; onRegionNameInput();`);
  const r = b.val("currentRegions")[0];
  assert.equal(r.region_type, "Full body"); assert.equal(r.region_name, "jane");
});

test("region tag helpers handle string + generated forms", () => {
  assert.equal(b.run(`rtagName("x")`), "x");
  assert.equal(b.run(`rtagName({tag:"y",generated:true})`), "y");
  assert.equal(b.run(`rtagIsConfirmed("x")`), true);
  assert.equal(b.run(`rtagIsPending({tag:"y",generated:true})`), true);
  assert.equal(b.run(`rtagIsPending({tag:"y",generated:true,confirmed:false})`), false);
  b.run(`currentRegions=[${JSON.stringify(box({ region_tags: [{ tag: "smile", generated: true }, "hat"] }))}]; selectRegion(0);
         acceptRegionTag(0); rejectRegionTag(0);`);
  // accept then reject → confirmed:false, record kept
  let t = b.val("currentRegions")[0].region_tags;
  assert.equal(t[0].confirmed, false); assert.equal(t.length, 2);
  b.run(`removeRegionTag(0)`);
  t = b.val("currentRegions")[0].region_tags;
  assert.deepEqual(t.map(x => rt(x)), ["hat"]);
  function rt(x) { return typeof x === "string" ? x : x.tag; }
});

test("renderRegionsList lists every box with confirm state", () => {
  b.run(`window.currentFile="x.jxl"; currentRegions=[${JSON.stringify(box())}, ${JSON.stringify(box({ confirmed: true, class_name: "cat" }))}]; renderRegionsList();`);
  const list = $("regions_list");
  assert.ok(!list.classList.contains("hidden"));
  assert.ok(list.innerHTML.includes("cat") && list.innerHTML.includes("person"));
  b.run(`currentRegions=[]; renderRegionsList();`);
  assert.ok(list.classList.contains("hidden"));
});
