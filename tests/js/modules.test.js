// Every enabled module's front-end JS evaluates on top of the core page
// without throwing, in the order the page would inject it.
const { page, test, assert, moduleAssets } = require("cim");

test("enabled module assets resolve to files", () => {
  const fs = require("fs");
  const missing = moduleAssets().filter(a => !fs.existsSync(a.file));
  assert.deepEqual(missing.map(a => a.url), []);
});

test("every module script evaluates cleanly", async () => {
  const b = page({ modules: true });
  assert.deepEqual(b.errors, []);
  await b.tick(50);
});

for (const id of [...new Set(moduleAssets().map(a => a.module_id))]) {
  test(`module ${id} loads alone on the core page`, () => {
    const b = page({ modules: [id] });
    assert.deepEqual(b.errors, []);
  });
}
