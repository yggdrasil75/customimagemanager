// The People tab's list: "+n" pages through every face of a person in place (the
// face tools keep working there), and face actions update the list in place
// instead of reloading it (which lost your place).
const { page, test, assert } = require("cim");

const face = (id, cid) => ({ id, rel: `p${id}.jxl`, cx: .5, cy: .5, w: .2, h: .2, dist: id / 1000 });

function open() {
  const b = page({ modules: ["people"] });
  for (const id of ["faces_list", "faces_status"]) {
    if (!b.document.getElementById(id)) {
      const d = b.document.createElement("div"); d.id = id; b.document.body.appendChild(d);
    }
  }
  b.run(`_faceClusters = ${JSON.stringify([
    { id: 1, name: "Big", count: 450, faces: [face(1), face(2), face(3)] },
    { id: 2, name: "Other", count: 3, faces: [face(901), face(902), face(903)] },
  ])};
  document.getElementById('faces_list').innerHTML = _faceClusters.map(c => _renderFaceCluster(c)).join('');`);
  return b;
}
const chips = (b, cid) => b.document.querySelectorAll(`#fcluster_${cid} [data-thumb]`).length;
const reloads = b => (b.api.calls || []).filter(c => String(c.url || c.path || "").startsWith("/api/faces/clusters")).length;

test("+n expands every face in place, a page at a time, least certain first", async () => {
  const b = open();
  const page1 = Array.from({ length: 200 }, (_, i) => face(1000 + i));
  b.api.on("/api/faces/cluster_faces", { success: true, total: 450, offset: 0, faces: page1 });
  assert.ok(b.document.querySelector("#fcluster_1").textContent.includes("+447"));
  await b.run("expandPersonFaces(1)");
  await b.tick(10);
  assert.equal(chips(b, 1), 200);
  const q = b.api.last("/api/faces/cluster_faces");
  assert.ok(String(q.url || q.path).includes("order=uncertain"));
  const text = b.document.getElementById("fcluster_1").textContent;
  assert.ok(text.includes("200 of 450 faces") && text.includes("Show 200 more (250 left)"));
  b.api.on("/api/faces/cluster_faces", { success: true, total: 450, offset: 200,
                                         faces: Array.from({ length: 200 }, (_, i) => face(2000 + i)) });
  await b.run("morePersonFaces(1)");
  await b.tick(10);
  assert.equal(chips(b, 1), 400);
  // a face dropped from the expanded person goes without a reload
  b.api.on("/api/faces/split", { success: true });
  await b.run("denyFace(2005)");
  await b.tick(10);
  assert.equal(chips(b, 1), 399);
  b.run("collapsePersonFaces(1)");
  assert.equal(chips(b, 1), 3);
});

test("changing a face's person moves the chip without reloading the list", async () => {
  const b = open();
  b.run(`CIMPeople.changeFace = async () => {
    window.dispatchEvent(new CustomEvent('cim:people-changed', { detail: { cluster_id: 2 } }));
    return { success: true, cluster_id: 2, name: 'Other' };
  };`);
  b.api.on("/api/faces/clusters", { clusters: [] });
  await b.run("changeFacePerson(2, 'p2.jxl')");
  await b.tick(10);
  assert.equal(b.api.last("/api/faces/clusters"), undefined, "no full reload");
  assert.equal(chips(b, 1), 2);
  assert.equal(chips(b, 2), 4);
  assert.ok(b.document.getElementById("faces_status").textContent.includes("Other"));
});
