// Stacks: the stack tile in the grid, the stack window, the bulk-bar and viewer actions.
const { page, test, assert, skipUnless } = require("cim");
const skip = skipUnless("stacks");

const STACK = { id: "s1", kind: "raw", cover: "a.jxl", auto: true, count: 2, members: [
  { filename: "a.jxl", width: 10, height: 10, cover: true, raw: null },
  { filename: "a_1.jxl", width: 10, height: 10, cover: false, raw: { uid: "u1", orig_name: "A.CR2" } }] };

async function boot() {
  const b = page({ modules: ["stacks"] });
  b.api.on("/api/stacks/s1", { success: true, stack: STACK });
  await b.tick(10);
  return b;
}
const $ = (b, s) => b.document.querySelector(s);

test("a stack cover renders as a layered tile with its count", { skip }, async () => {
  const b = await boot();
  assert.deepEqual(b.errors, []);
  b.run(`renderGallery([
    {filename:"a.jxl",kind:"image",width:10,height:10,tags:[],description:"",stack:{id:"s1",kind:"raw",count:2}},
    {filename:"b.jxl",kind:"image",width:10,height:10,tags:[],description:""}])`);
  const tiles = b.document.querySelectorAll("#gallery_grid .gallery-item");
  assert.equal(tiles.length, 2);
  assert.ok(tiles[0].classList.contains("cim-stack"));
  assert.ok(!tiles[1].classList.contains("cim-stack"));
  assert.equal(tiles[0].querySelector(".cim-stack-badge").textContent.trim(), "2");
});

test("the badge opens the stack window with its members and raw link", { skip }, async () => {
  const b = await boot();
  b.run(`renderGallery([{filename:"a.jxl",kind:"image",width:10,height:10,tags:[],description:"",stack:{id:"s1",kind:"raw",count:2}}])`);
  $(b, "#gallery_grid .cim-stack-badge").click();
  await b.tick(20);
  const modal = b.document.getElementById("stacks_modal");
  assert.ok(modal && !modal.classList.contains("hidden"));
  const members = modal.querySelectorAll(".cim-stack-member");
  assert.equal(members.length, 2);
  assert.equal(members[0].dataset.filename, "a.jxl");
  assert.ok(members[0].classList.contains("cim-stack-cover"));
  assert.equal(modal.querySelector("a.cim-stack-raw").getAttribute("href"), "/api/raw/open/u1");
});

test("unstack posts and reloads the grid", { skip }, async () => {
  const b = await boot();
  b.api.on("POST /api/stacks/s1/unstack", { success: true });
  b.api.on("/api/list", { success: true, files: [], total: 0, page: 0, page_size: 200 });
  b.run(`CIMStacks.openStack("s1")`);
  await b.tick(20);
  b.document.getElementById("stacks_btn_unstack").click();
  await b.tick(20);
  assert.ok(b.api.last("/api/stacks/s1/unstack", "POST"));
  assert.ok(b.document.getElementById("stacks_modal").classList.contains("hidden"));
  assert.ok(b.api.last("/api/list"));
});

test("stack selected sends the selection, cover = the open file", { skip }, async () => {
  const b = await boot();
  b.api.on("POST /api/stacks/create", { success: true, stack: STACK });
  b.api.on("/api/list", { success: true, files: [], total: 0, page: 0, page_size: 200 });
  b.run(`selectedFiles.clear(); selectedFiles.add("x.jxl"); selectedFiles.add("y.jxl"); window.currentFile="y.jxl";
         CIMStacks.stackSelected();`);
  await b.tick(20);
  const body = b.api.last("/api/stacks/create", "POST").body;
  assert.deepEqual(body.filenames, ["x.jxl", "y.jxl"]);
  assert.equal(body.cover, "y.jxl");
});

test("viewer buttons follow the open file's stack and animation", { skip }, async () => {
  const b = await boot();
  b.api.on("/api/stacks/of", c => (c.query.filename === "anim.jxl"
    ? { success: true, stack: null, animated: true }
    : { success: true, stack: STACK, animated: false }));
  b.run(`window.currentFile="a.jxl"; CIMStacks.refreshViewer("a.jxl")`);
  await b.tick(20);
  const open = $(b, ".stacks-open-btn"), split = $(b, ".stacks-split-btn");
  assert.ok(open && split);
  assert.equal(open.style.display, "");
  assert.equal(open.textContent, "Stack (2)");
  assert.equal(split.style.display, "none");
  b.run(`window.currentFile="anim.jxl"; CIMStacks.refreshViewer("anim.jxl")`);
  await b.tick(20);
  assert.equal(open.style.display, "none");
  assert.equal(split.style.display, "");
});