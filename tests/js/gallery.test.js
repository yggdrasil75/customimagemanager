const { page, test, assert } = require("./_setup");
const b = page();
const $ = id => b.document.getElementById(id);

test("renderGallery builds one tile per file, books/comics excluded from selection set", () => {
  b.run(`renderGallery([
    {filename:"a.jxl",kind:"image",width:10,height:10,tags:[],description:""},
    {filename:"b.jxl",kind:"image",width:10,height:10,tags:["?x"],description:""},
    {kind:"comic",folder:"c",title:"C",page_count:3,cover:"",width:1,height:2},
    {kind:"book",rel_path:"b.epub",has_cover:false,book_kind:"book",title:"B",tags:[],fmt:"epub"}])`);
  const tiles = $("gallery_grid").querySelectorAll(".gallery-item");
  assert.equal(tiles.length, 4);
  assert.equal(b.val("galleryFiles").length, 2);
  assert.equal(tiles[2].dataset.kind, "comic");
  assert.equal(tiles[3].dataset.kind, "book");
});

test("changePage re-queries with the new page", async () => {
  b.api.on("/api/list", { success: true, files: [], total: 1000, page: 0, page_size: 200 });
  b.run(`currentPage=0; totalFiles=1000; changePage(1);`);
  await b.tick(10);
  assert.equal(b.api.last("/api/list").query.page, "1");
  assert.equal(b.run("currentPage"), 1);
});

test("search failure shows a toast and clears the grid", async () => {
  b.api.on("/api/list", { success: false, error: "no embeddings", files: [], total: 0 });
  b.run(`currentSearch="sem:cat"; loadGallery();`);
  await b.tick(10);
  assert.equal($("gallery_grid").children.length, 0);
  assert.equal(b.run("totalFiles"), 0);
  b.api.on("/api/list", { success: true, files: [], total: 0, page: 0, page_size: 200 });
});

test("_stripDateTokens drops date:* tokens only", () => {
  assert.deepEqual([...b.val(`_stripDateTokens("cat date:2024 is:tagged modified:>2023")`)], ["cat", "is:tagged"]);
});

test("selectFile reads metadata and populates tags/regions/description", async () => {
  b.api.on("POST /api/metadata", c => c.body.action === "read"
    ? { success: true, metadata: { tags: ["cat", "?dog"], description: "hello", regions: [{ class_name: "person", region_type: "person", region_name: "", cx: .5, cy: .5, w: .4, h: .8, confirmed: false, region_tags: [] }], albums: [] } }
    : { success: true });
  b.run(`selectFile("pic.jxl")`);
  await b.tick(30);
  assert.equal(b.run("window.currentFile"), "pic.jxl");
  assert.deepEqual(b.val("currentTags"), ["cat", "?dog"]);
  assert.equal(b.val("currentRegions").length, 1);
  assert.equal($("meta_desc").value, "hello");
  assert.equal(b.api.last("/api/metadata", "POST").body.filename, "pic.jxl");
});
