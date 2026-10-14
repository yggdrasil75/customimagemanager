// Template for a module's own frontend tests: modules/<id>/tests/*.test.js.
// `require("cim")` gives the jsdom page harness (tests/js/cim.js). Boot with
// {modules: ["<id>"]} to load your module's JS on top of the real core page;
// b.api scripts/records fetch() so no server is needed.
//
// This module ships no JS: its settings block is declared in module.py
// (host.add_settings_section) and drawn by the core's static/module_ui.js, so
// the test checks that the declaration reached the page and works.
const { page, test, assert, skipUnless } = require("cim");
const skip = skipUnless("example_hello");

test("declared settings block renders when its tab opens, and its button calls the module", { skip }, async () => {
  const b = page({ modules: ["example_hello"] });
  assert.deepEqual(b.errors, []);
  const pane = b.document.createElement("div");
  pane.id = "settings_pane_module_example_hello";
  b.document.body.appendChild(pane);
  await b.tick(10);
  b.api.on("/api/hello", { ok: true, module: "example_hello", message: "Hello from a pluggable module.",
    view: { rows: [{ label: "Page size", value: 200 }] } });
  b.document.dispatchEvent(new b.window.CustomEvent("module-settings-tab", { detail: "example_hello" }));
  await b.tick(20);
  const btn = pane.querySelector('[data-ui-action="ping"]');
  assert.ok(btn, "ping button rendered from the declaration");
  assert.match(pane.textContent, /Page size/);
  btn.click();
  await b.tick(20);
  assert.ok(b.api.last("/api/hello", "POST"), "clicked -> POST /api/hello");
  assert.match(pane.querySelector("[data-ui-msg]").textContent, /Hello from a pluggable module/);
});