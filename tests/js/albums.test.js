const { page, test, assert } = require("./_setup");
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
