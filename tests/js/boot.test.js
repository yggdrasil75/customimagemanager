const { page, test, assert } = require("./_setup");
const b = page();

test("every core script evaluates without throwing", () => {
  assert.deepEqual(b.errors, []);
});

test("startup hits the expected endpoints", async () => {
  await b.tick(20);
  const paths = b.api.calls.map(c => c.path);
  for (const p of ["/api/state", "/api/folders", "/api/albums", "/api/list", "/api/review_list"])
    assert.ok(paths.includes(p), p);
});

test("applyBranding updates header + title", () => {
  b.run(`applyBranding({brand_name:"MyLib", brand_logo:""})`);
  assert.equal(b.document.title, "MyLib");
  const h1 = b.document.getElementById("brand_name_h1");
  if (h1) assert.equal(h1.textContent, "MyLib");
});

test("url params seed search/page state", async () => {
  const c = page({ url: "http://t/?q=cat&page=2&folder=sub" });
  await c.tick(10);
  assert.equal(c.run("currentSearch"), "cat");
  assert.equal(c.run("currentPage"), 2);
  const list = c.api.last("/api/list");
  assert.equal(list.query.q, "cat"); assert.equal(list.query.page, "2"); assert.equal(list.query.folder, "sub");
});
