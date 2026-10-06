// Core theming front end: body attributes, set() through user settings,
// re-applied feature gates, theme.choose.
const { page, test, assert, skipUnless } = require("cim");
const skip = skipUnless("theming");

const THEMES = { success: true, can_choose: true,
  themes: { layout: [{ id: "advanced", label: "Advanced" }, { id: "simple", label: "Simple" }],
            palette: [{ id: "blue", label: "Blue" }, { id: "orange", label: "Orange" }] },
  selected: { layout: "advanced", palette: "blue" }, defaults: { layout: "advanced", palette: "blue" } };

test("loads /api/theme and sets body attributes; no header picker", { skip }, async () => {
  const b = page({ modules: ["theming"] });
  assert.deepEqual(b.errors, []);
  b.api.on("/api/theme", THEMES);
  await b.tick(30);
  assert.equal(b.document.body.dataset.layout, "advanced");
  assert.equal(b.document.body.dataset.palette, "blue");
  assert.equal(b.document.getElementById("theme_pick_functional"), null);
});

test("set() saves the user setting, reloads and fires cim:theme", { skip }, async () => {
  const b = page({ modules: ["theming"] });
  let sel = { layout: "advanced", palette: "blue" };
  b.api.on("/api/theme", () => Object.assign({}, THEMES, { selected: sel }));
  b.api.on("POST /api/user/settings", c => { sel = Object.assign({}, sel, c.body); return { success: true, fields: [] }; });
  await b.tick(30);
  let fired = null;
  b.window.addEventListener("cim:theme", e => { fired = e.detail; });
  await b.run(`CIMTheme.set("palette", "orange")`);
  assert.equal(b.api.last("/api/user/settings", "POST").body.palette, "orange");
  assert.equal(b.document.body.dataset.palette, "orange");
  assert.deepEqual(JSON.parse(JSON.stringify(fired)), { layout: "advanced", palette: "orange" });
});

test("saving user settings with a theme key reloads the theme", { skip }, async () => {
  const b = page({ modules: ["theming"] });
  let sel = { layout: "advanced", palette: "blue" };
  b.api.on("/api/theme", () => Object.assign({}, THEMES, { selected: sel }));
  await b.tick(30);
  sel = { layout: "simple", palette: "blue" };
  b.window.dispatchEvent(new b.window.CustomEvent("cim:user-settings", { detail: { keys: ["layout"] } }));
  await b.tick(30);
  assert.equal(b.document.body.dataset.layout, "simple");
});

test("a theme switch re-applies feature gates", { skip }, async () => {
  const b = page({ modules: ["theming"] });
  b.api.on("/api/theme", THEMES);
  await b.tick(30);
  b.run(`CIMAuth.user.features = Object.assign({}, CIMAuth.user.features, {"data.delete": 0});`);
  const el = b.document.querySelector('[data-feature="data.delete"]');
  el.classList.remove("cim-feature-hidden");
  b.window.dispatchEvent(new b.window.CustomEvent("cim:user-settings", { detail: { keys: ["layout"] } }));
  await b.tick(30);
  assert.ok(el.classList.contains("cim-feature-hidden"));
});

test("without theme.choose set() refuses and posts nothing", { skip }, async () => {
  const b = page({ modules: ["theming"] });
  b.api.on("/api/theme", Object.assign({}, THEMES, { can_choose: false }));
  await b.tick(30);
  const r = JSON.parse(JSON.stringify(await b.run(`CIMTheme.set("palette", "orange")`)));
  assert.equal(r.ok, false);
  assert.equal(b.api.find("/api/user/settings", "POST").length, 0);
});