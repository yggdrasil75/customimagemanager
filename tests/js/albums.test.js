const { page, test, assert } = require("cim");
const b = page();

test("loadImageAlbums renders rows from /api/albums", async () => {
  b.api.on("/api/albums", { success: true, albums: [{ name: "Trip", count: 3, cover: "", description: "" }, { name: "Work", count: 0, cover: "", description: "" }] });
  b.run(`loadImageAlbums()`);
  await b.tick(20);
  const pane = b.document.getElementById("albums_list") || b.document.getElementById("albums_pane");
  assert.ok(pane);
  assert.ok(pane.textContent.includes("Trip") && pane.textContent.includes("Work"));
});

test("createAlbumPrompt posts the typed name", async () => {
  const c = page({ prompt: "Holiday" });
  await c.tick(10);
  c.run(`createAlbumPrompt()`);
  await c.tick(20);
  const call = c.api.last("/api/albums/create", "POST");
  assert.ok(call, "POST /api/albums/create");
  assert.equal(call.body.name, "Holiday");
});

test("album rows show the description under the name and the sort order", async () => {
  const c = page();
  c.api.on("/api/albums", { success: true, albums: [
    { name: "Trip", count: 2, cover: "", description: "Summer by the sea", sort: "-taken" }] });
  c.run(`loadImageAlbums()`);
  await c.tick(20);
  const row = c.document.getElementById("albums_list").firstElementChild;
  const lines = [...row.querySelectorAll("div.flex-1 > div")].map(d => d.textContent);
  assert.equal(lines[0], "Trip");
  assert.equal(lines[1], "Summer by the sea");
  assert.ok(row.querySelector(".album-sort").textContent.includes("newest first"));
});

test("createAlbumPrompt also sends the description", async () => {
  const c = page({ prompt: "Holiday" });
  await c.tick(10);
  c.run(`createAlbumPrompt()`);
  await c.tick(20);
  const call = c.api.last("/api/albums/create", "POST");
  assert.equal(call.body.description, "Holiday");
});

test("the album banner shows the description and sort, and saves both", async () => {
  const c = page();
  c.api.on("/api/albums", { success: true, albums: [
    { name: "Trip", count: 2, cover: "", description: "Old text", sort: "added" }] });
  c.api.on("/api/albums/describe", (call) => ({ success: true, description: call.body.description }));
  c.api.on("/api/albums/sort", (call) => ({ success: true, sort: call.body.sort }));
  await c.tick(10);
  c.run(`loadImageAlbums().then(() => openAlbumGallery("Trip"))`);
  await c.tick(30);
  assert.equal(c.document.getElementById("gallery_album_desc").textContent, "Old text");
  const sel = c.document.getElementById("gallery_album_sort");
  assert.equal(sel.value, "added");
  assert.ok(sel.options.length >= 8);
  // inline edit: the pencil swaps in an input, Enter saves
  c.run(`albumEditDescription()`);
  const inp = c.document.getElementById("gallery_album_desc_input");
  assert.ok(inp);
  inp.value = "New text";
  inp.dispatchEvent(new c.window.KeyboardEvent("keydown", { key: "Enter" }));
  await c.tick(20);
  assert.deepEqual(c.api.last("/api/albums/describe", "POST").body, { album: "Trip", description: "New text" });
  assert.equal(c.document.getElementById("gallery_album_desc").textContent, "New text");
  c.run(`albumSetSort("manual")`);
  await c.tick(20);
  assert.deepEqual(c.api.last("/api/albums/sort", "POST").body, { album: "Trip", sort: "manual" });
  assert.equal(c.api.last("/api/list", "GET").query.album, "Trip");
});

test("dragging a tile in an album posts the new page order", async () => {
  const c = page();
  c.api.on("/api/albums", { success: true, albums: [{ name: "Trip", count: 3, cover: "", description: "", sort: "" }] });
  c.api.on("/api/list", { success: true, total: 3, page: 0, page_size: 200, files: ["a.jxl", "b.jxl", "c.jxl"].map(
    f => ({ kind: "image", filename: f, tags: [], description: "", width: 10, height: 10 })) });
  await c.tick(10);
  c.run(`loadImageAlbums().then(() => openAlbumGallery("Trip"))`);
  await c.tick(40);
  assert.deepEqual(c.val(`albumMovedOrder(["a","b","c"], "c", "a")`), ["c", "a", "b"]);
  assert.deepEqual(c.val(`albumMovedOrder(["a","b","c"], "a", "c")`), ["b", "c", "a"]);
  const tiles = [...c.document.querySelectorAll("#gallery_grid .gallery-item")];
  assert.equal(tiles.length, 3);
  assert.equal(tiles[0].draggable, true);
  const ev = (type) => { const e = new c.window.Event(type, { bubbles: true, cancelable: true }); e.dataTransfer = { setData() {}, effectAllowed: "" }; return e; };
  tiles[2].dispatchEvent(ev("dragstart"));
  tiles[0].dispatchEvent(ev("dragover"));
  tiles[0].dispatchEvent(ev("drop"));
  await c.tick(20);
  assert.deepEqual(c.api.last("/api/albums/order", "POST").body, { album: "Trip", rel_paths: ["c.jxl", "a.jxl", "b.jxl"] });
});
