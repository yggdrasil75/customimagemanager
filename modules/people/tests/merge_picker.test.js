// The People tab's merge picker: same / similar names first (never merged by name
// alone), then the most alike faces; the confirm step shows both people and warns
// when only a first name matches.
const { page, test, assert } = require("cim");

function open() {
  const b = page({ modules: ["people"] });
  const face = (rel) => [{ id: 1, rel, cx: .5, cy: .5, w: .2, h: .2 }];
  b.run(`_faceClusters = ${JSON.stringify([
    { id: 1, name: "Sam Carter", count: 40, faces: face("a.jxl") },
    { id: 2, name: "Sam Okafor", count: 30, faces: face("b.jxl") },
    { id: 3, name: "sam  carter", count: 5, faces: face("c.jxl") },
    { id: 4, name: "", count: 201, faces: face("d.jxl") },
    { id: 5, name: "", count: 97, faces: face("e.jxl") },
    { id: 6, name: "Samantha Carter", count: 9, faces: face("f.jxl") },
    { id: 7, name: "Bob", count: 12, faces: face("g.jxl") },
  ])};`);
  if (!b.document.getElementById("faces_status")) {
    const s = b.document.createElement("div"); s.id = "faces_status"; b.document.body.appendChild(s);
  }
  b.api.on("/api/faces/merge_candidates", { success: true, similar: { 2: 0.12, 3: 0.8, 4: 0.1, 5: 0.71, 6: 0.2, 7: 0.05 } });
  return b;
}
const rows = b => [...b.document.querySelectorAll("#merge_list [data-merge-src]")].map(x => +x.dataset.mergeSrc);

test("name relations: same, first name only, similar, none", () => {
  const b = page({ modules: ["people"] });
  assert.equal(b.run(`_nameRelation("Sam Carter", "sam  cárter")`), "same");
  assert.equal(b.run(`_nameRelation("Sam Carter", "Sam Okafor")`), "first");
  assert.equal(b.run(`_nameRelation("Sam Carter", "Samantha Carter")`), "similar");
  assert.equal(b.run(`_nameRelation("Katherine", "Katharine")`), "similar");
  assert.equal(b.run(`_nameRelation("Sam Carter", "Bob")`), "");
  assert.equal(b.run(`_nameRelation("Sam Carter", "")`), "");
});

test("names lead, then the most alike faces; unnamed people are not listed first", async () => {
  const b = open();
  await b.run("mergeInto(1)");
  await b.tick(10);
  assert.deepEqual(rows(b), [3, 2, 6, 5, 4, 7]);
  const text = b.document.getElementById("merge_list").textContent;
  assert.ok(text.includes("Same name") && text.includes("Same first name") && text.includes("Similar name"));
  // the search narrows the list by name
  const f = b.document.getElementById("merge_filter");
  f.value = "okafor"; f.dispatchEvent(new b.window.Event("input"));
  assert.deepEqual(rows(b), [2]);
});

test("first name only: the confirm step warns, shows both, and merges only on the button", async () => {
  const b = open();
  b.api.on("/api/faces/merge", { success: true, moved: 30 });
  await b.run("mergeInto(1)");
  await b.tick(10);
  b.document.querySelector('[data-merge-src="2"]').click();
  const dlg = b.document.getElementById("merge_dialog");
  assert.ok(dlg.textContent.includes("Only the first name matches"));
  assert.equal(b.api.last("/api/faces/merge", "POST"), undefined);
  dlg.querySelector("[data-merge-go]").click();
  await b.tick(10);
  const call = b.api.last("/api/faces/merge", "POST");
  assert.deepEqual([call.body.src, call.body.dst, call.body.confirm], [2, 1, true]);
  assert.equal(b.document.getElementById("merge_dialog"), null);
});
