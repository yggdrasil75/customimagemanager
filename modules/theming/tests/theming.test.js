// Core theming front end: body attributes, set() through user settings,
// re-applied feature gates, theme.choose, the colour scheme (matchMedia).
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
  assert.deepEqual(JSON.parse(JSON.stringify(fired)), { layout: "advanced", palette: "orange", scheme: "dark" });
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

/** @brief A controllable prefers-color-scheme: light query for the page's window. */
function fakeMedia(b, light) {
  const mq = { matches: light, listeners: [], addEventListener(t, fn) { this.listeners.push(fn); } };
  b.window.matchMedia = q => { mq.query = q; return mq; };
  return { mq, flip(v) { mq.matches = v; mq.listeners.forEach(fn => fn({ matches: v })); } };
}

test("scheme auto follows prefers-color-scheme, live", { skip }, async () => {
  const b = page({ modules: ["theming"] });
  const m = fakeMedia(b, true);
  b.api.on("/api/theme", Object.assign({}, THEMES, { selected: Object.assign({}, THEMES.selected, { scheme: "auto" }) }));
  b.window.CIMTheme.refresh();
  await b.tick(30);
  assert.equal(m.mq.query, "(prefers-color-scheme: light)");
  assert.equal(b.document.body.dataset.scheme, "light");
  assert.equal(b.document.documentElement.style.colorScheme, "light");
  assert.equal(b.run("CIMTheme.scheme"), "auto");
  assert.equal(b.run("CIMTheme.effectiveScheme"), "light");
  assert.equal(b.run("localStorage.getItem('cim.scheme')"), "auto");
  let fired = null;
  b.window.addEventListener("cim:theme", e => { fired = e.detail; });
  m.flip(false);
  assert.equal(b.document.body.dataset.scheme, "dark");
  assert.equal(fired && fired.scheme, "dark");
});

test("an explicit scheme ignores the browser; set('scheme') saves it", { skip }, async () => {
  const b = page({ modules: ["theming"] });
  const m = fakeMedia(b, true);
  let sel = Object.assign({}, THEMES.selected, { scheme: "dark" });
  b.api.on("/api/theme", () => Object.assign({}, THEMES, { selected: sel }));
  b.api.on("POST /api/user/settings", c => { sel = Object.assign({}, sel, c.body); return { success: true, fields: [] }; });
  b.window.CIMTheme.refresh();
  await b.tick(30);
  assert.equal(b.document.body.dataset.scheme, "dark");
  m.flip(false); m.flip(true);
  assert.equal(b.document.body.dataset.scheme, "dark");
  await b.run(`CIMTheme.set("scheme", "light")`);
  assert.equal(b.api.last("/api/user/settings", "POST").body.scheme, "light");
  assert.equal(b.document.body.dataset.scheme, "light");
  assert.equal(b.run("localStorage.getItem('cim.scheme')"), "light");
});

test("without matchMedia auto falls back to dark", { skip }, async () => {
  const b = page({ modules: ["theming"] });
  b.api.on("/api/theme", THEMES);
  await b.tick(30);
  assert.equal(b.document.body.dataset.scheme, "dark");
  assert.equal(b.run("CIMTheme.scheme"), "auto");
});
