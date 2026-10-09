// The rank / filter switch in the search box: sem:<text> <-> about:<text>.
const { page, test, assert, skipUnless } = require("cim");
const skip = skipUnless("embedding");

test("switch shows for sem: / about: and rewrites one into the other", { skip }, async () => {
  const b = page({ modules: ["embedding"] });
  await b.tick(10);
  assert.deepEqual(b.errors, []);
  const si = b.document.getElementById("search_input");
  const btn = () => b.document.getElementById("sem_mode_btn");
  const type = v => { si.value = v; si.dispatchEvent(new b.window.Event("input", { bubbles: true })); };
  assert.ok(btn(), "button registered in search_tools");
  type("tag:cat");
  assert.equal(btn().style.display, "none");
  type("sem:red car -dog");
  assert.notEqual(btn().style.display, "none");
  assert.equal(btn().textContent, "Filter");
  btn().click();
  assert.equal(si.value, "about:red_car_-dog");
  assert.equal(btn().textContent, "Rank");
  btn().click();
  assert.equal(si.value, "sem:red car -dog");
  type("~beach");
  btn().click();
  assert.equal(si.value, "about:beach");
  type("about:beach sort:-date tag:x");                     // ranking drops the other tokens
  btn().click();
  assert.equal(si.value, "sem:beach");
});
