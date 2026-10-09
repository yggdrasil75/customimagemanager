// Image-to-image search popover in the gallery search box (image_search.js).
const { page, test, assert, skipUnless } = require("cim");
const skip = skipUnless("embedding");

const RESULT = { success: true, results: [{ filename: "hit.jxl", score: 0.9 }],
  files: [{ kind: "image", filename: "hit.jxl", tags: [], description: "", width: 10, height: 10, score: 0.9 }] };

function boot() {
  const b = page({ modules: ["embedding"] });
  b.api.on("POST /api/embedding/search_image", RESULT);
  return b;
}

test("picking a file posts it and fills the grid", { skip }, async () => {
  const b = boot();
  assert.deepEqual(b.errors, []);
  const $ = id => b.document.getElementById(id);
  assert.ok($("is_btn"), "image icon in the search box");
  $("is_btn").click();
  assert.ok(!$("is_pop").classList.contains("hidden"));
  const file = new b.window.File(["png"], "query.png", { type: "image/png" });
  const input = $("is_file");
  Object.defineProperty(input, "files", { value: [file], configurable: true });
  input.dispatchEvent(new b.window.Event("change", { bubbles: true }));
  await b.tick(20);
  const call = b.api.last("/api/embedding/search_image", "POST");
  assert.ok(call, "POST /api/embedding/search_image");
  assert.ok(call.body instanceof b.window.FormData, "multipart body");
  assert.equal(call.body.get("file").name, "query.png");
  assert.equal(call.body.get("top_k"), "60");
  assert.ok($("gallery_grid").querySelector('[data-filename="hit.jxl"]'), "result tile rendered");
  assert.ok($("is_pop").classList.contains("hidden"));
});

test("dropping an image on the search box searches with it", { skip }, async () => {
  const b = boot();
  const $ = id => b.document.getElementById(id);
  const file = new b.window.File(["png"], "dropped.png", { type: "image/png" });
  const ev = new b.window.Event("drop", { bubbles: true, cancelable: true });
  ev.dataTransfer = { files: [file], items: [], types: ["Files"] };
  $("search_input").dispatchEvent(ev);
  await b.tick(20);
  assert.ok(ev.defaultPrevented, "the browser does not open the file");
  const call = b.api.last("/api/embedding/search_image", "POST");
  assert.ok(call && call.body.get("file").name === "dropped.png");
});

test("use current picture posts the open file by name", { skip }, async () => {
  const b = boot();
  const $ = id => b.document.getElementById(id);
  b.run(`window.currentFile = "cur.jxl";`);
  $("is_btn").click();
  $("is_current").click();
  await b.tick(20);
  const call = b.api.last("/api/embedding/search_image", "POST");
  assert.deepEqual(JSON.parse(JSON.stringify(call.body)), { filename: "cur.jxl", top_k: 60 });
});
