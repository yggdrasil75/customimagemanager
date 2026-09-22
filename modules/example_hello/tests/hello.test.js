// Template for a module's own frontend tests: modules/<id>/tests/*.test.js.
// `require("cim")` gives the jsdom page harness (tests/js/cim.js). Boot with
// {modules: ["<id>"]} to load your module's JS on top of the real core page;
// b.api scripts/records fetch() so no server is needed.
const { page, test, assert, skipUnless } = require("cim");
const skip = skipUnless("example_hello");

test("settings pane renders when its tab opens, and pings /api/hello", { skip }, async () => {
  const b = page({ modules: ["example_hello"] });
  assert.deepEqual(b.errors, []);
  const pane = b.document.createElement("div");
  pane.id = "settings_pane_module_example_hello";
  b.document.body.appendChild(pane);
  b.api.on("/api/hello", { ok: true, module: "example_hello", message: "hi" });
  b.document.dispatchEvent(new b.window.CustomEvent("module-settings-tab", { detail: "example_hello" }));
  await b.tick(10);
  const btn = pane.querySelector("#hello_ping");
  assert.ok(btn, "ping button rendered");
  btn.click();
  await b.tick(20);
  assert.ok(b.api.last("/api/hello"), "clicked -> GET /api/hello");
  assert.match(pane.querySelector("#hello_out").textContent, /"module": "example_hello"/);
});
