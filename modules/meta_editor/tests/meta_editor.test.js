// meta_editor front-end: buttons register, bulk rotate / date / location post the
// right bodies, the crop editor posts the rectangle.
const { page, test, assert, skipUnless } = require("cim");
const skip = skipUnless("meta_editor");

test("buttons land in the viewer toggles and the bulk bar", { skip }, async () => {
  const b = page({ modules: ["meta_editor"] });
  assert.deepEqual(b.errors, []);
  const html = b.val("[...document.querySelectorAll('[data-ext-area]')].map(e => e.innerHTML).join(' ')");
  for (const want of ["metaRotate('current','left')", "metaCropStart()", "metaLocation('current')",
                      "metaDates('selection')", "metaLocation('selection')", "metaRotate('selection','right')"])
    assert.ok(html.includes(want), want);
});

test("bulk rotate, shift dates and location post the selection", { skip }, async () => {
  const b = page({ modules: ["meta_editor"] });
  b.api.on("POST /api/meta_editor/rotate", { success: true, done: 2, errors: {}, versions: { "a.jxl": 3 } });
  b.api.on("POST /api/meta_editor/dates", { success: true, done: 2, errors: {}, versions: {} });
  b.api.on("POST /api/meta_editor/location", { success: true, done: 2, errors: {}, versions: {} });
  b.run("selectedFiles.clear(); selectedFiles.add('a.jxl'); selectedFiles.add('b.jxl');");
  await b.run("metaRotate('selection', 'right')");
  const rot = b.api.last("/api/meta_editor/rotate", "POST");
  assert.deepEqual(JSON.parse(JSON.stringify(rot.body)), { filenames: ["a.jxl", "b.jxl"], direction: "right" });

  await b.run("metaDates('selection')");
  const body = b.document.querySelector(".me-modal-body");
  body.querySelector('input[value="shift"]').checked = true;
  body.querySelector('input[value="shift"]').dispatchEvent(new b.window.Event("change"));
  body.querySelector(".me-sign").value = "-1";
  body.querySelector(".me-h").value = "2";
  body.querySelector(".me-m").value = "30";
  body.querySelector(".me-apply").click();
  await b.tick(10);
  const d = b.api.last("/api/meta_editor/dates", "POST").body;
  assert.equal(d.mode, "shift");
  assert.equal(d.shift_seconds, -(2 * 3600 + 30 * 60));

  await b.run("metaLocation('selection')");
  const lb = b.document.querySelector(".me-modal-body");
  const paste = lb.querySelector(".me-paste");
  paste.value = "https://www.openstreetmap.org/#map=17/48.85840/2.29450";
  paste.dispatchEvent(new b.window.Event("input"));
  assert.equal(lb.querySelector(".me-lat").value, "48.858400");
  lb.querySelector(".me-save").click();
  await b.tick(10);
  const loc = b.api.last("/api/meta_editor/location", "POST").body;
  assert.equal(loc.lat, 48.8584);
  assert.equal(loc.lon, 2.2945);
  assert.equal(loc.filenames.length, 2);
});

test("crop mode posts the rectangle for the open file", { skip }, async () => {
  const b = page({ modules: ["meta_editor"] });
  b.api.on("POST /api/meta_editor/crop", { success: true, done: 1, errors: {}, versions: { "c.jxl": 1 } });
  b.run("window.currentFile = 'c.jxl'; Object.defineProperty(imgObj, 'naturalWidth', {value: 400}); " +
        "Object.defineProperty(imgObj, 'naturalHeight', {value: 200});");
  b.run("metaCropStart()");
  assert.ok(b.document.querySelector(".me-crop-layer"), "crop layer shown");
  const sel = b.document.querySelector(".me-aspect");
  sel.value = "1:1";
  sel.dispatchEvent(new b.window.Event("change"));
  b.run("metaCropApply()");
  await b.tick(10);
  const c = b.api.last("/api/meta_editor/crop", "POST").body;
  assert.equal(c.filename, "c.jxl");
  // 1:1 on a 2:1 frame: the normalised width is half the normalised height
  const w = c.crop.right - c.crop.left, h = c.crop.bottom - c.crop.top;
  assert.ok(Math.abs(w * 400 - h * 200) < 1e-6, JSON.stringify(c.crop));
  assert.equal(b.document.querySelector(".me-crop-layer"), null, "crop layer closed");
});
