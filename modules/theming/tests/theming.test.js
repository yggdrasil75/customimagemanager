// Core theming front end: applies body attributes, pickers, re-applies gates.
const { page, test, assert, skipUnless } = require("cim");
const skip = skipUnless("theming");

const THEMES = { success: true, can_choose: true,
  themes: { functional: [{ id: "advanced", label: "Advanced" }, { id: "simple", label: "Simple" }],
            colorings: [{ id: "blue", label: "Blue" }, { id: "orange", label: "Orange" }] },
  selected: { functional: "advanced", colorings: "blue" }, chosen: {}, defaults: { functional: "advanced", colorings: "blue" } };

test("loads /api/theme, sets body attributes and renders pickers", { skip }, async () => {
  const b = page({ modules: ["theming"] });
  assert.deepEqual(b.errors, []);
  b.api.on("/api/theme", THEMES);
  await b.tick(30);
  assert.equal(b.document.body.dataset.functional, "advanced");
  assert.equal(b.document.body.dataset.colorings, "blue");
  assert.equal(b.document.getElementById("theme_pick_functional").value, "advanced");
  assert.equal(b.document.getElementById("theme_pick_colorings_cfg").value, "blue");
});

test("set() posts, applies the server's answer and fires cim:theme", { skip }, async () => {
  const b = page({ modules: ["theming"] });
  b.api.on("/api/theme", c => c.method === "POST"
    ? { success: true, selected: { functional: "simple", colorings: c.body.colorings || "blue" }, chosen: c.body, defaults: THEMES.defaults, can_choose: true }
    : THEMES);
  await b.tick(30);
  let fired = null;
  b.window.addEventListener("cim:theme", e => { fired = e.detail; });
  await b.run(`CIMTheme.set("colorings", "orange")`);
  assert.equal(b.api.last("/api/theme", "POST").body.colorings, "orange");
  assert.equal(b.document.body.dataset.colorings, "orange");
  assert.deepEqual(JSON.parse(JSON.stringify(fired)), { functional: "simple", colorings: "orange" });
});

test("a theme switch re-applies feature gates", { skip }, async () => {
  const b = page({ modules: ["theming"] });
  b.api.on("/api/theme", THEMES);
  await b.tick(30);
  // Deny a feature, then pretend a theme un-hid the element: the switch must re-hide it.
  b.run(`CIMAuth.user.features = Object.assign({}, CIMAuth.user.features, {"data.delete": 0});`);
  const el = b.document.querySelector('[data-feature="data.delete"]');
  el.classList.remove("cim-feature-hidden");
  b.api.on("/api/theme", c => c.method === "POST"
    ? { success: true, selected: { functional: "simple", colorings: "blue" }, chosen: {}, defaults: THEMES.defaults, can_choose: true } : THEMES);
  await b.run(`CIMTheme.set("functional", "simple")`);
  assert.ok(el.classList.contains("cim-feature-hidden"));
});

test("without theme.choose the pickers are disabled and set() refuses", { skip }, async () => {
  const b = page({ modules: ["theming"] });
  b.api.on("/api/theme", Object.assign({}, THEMES, { can_choose: false }));
  await b.tick(30);
  assert.equal(b.document.getElementById("theme_pick_colorings_cfg").disabled, true);
  const r = JSON.parse(JSON.stringify(await b.run(`CIMTheme.set("colorings", "orange")`)));
  assert.equal(r.ok, false);
  assert.equal(b.api.find("/api/theme", "POST").length, 0);
});