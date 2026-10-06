// Settings modal: per-tab permissions (hidden / read-only / writable), the
// grouped rail, the generic field widgets and Settings → User settings.
const { page, test, assert } = require("cim");

const FIELDS = { modules: [], settings_tabs: [
    { id: "pipeline", label: "Pipeline", icon: "✨", admin_only: true, group: "modules", feature: "settings.pipeline", module_id: "pipeline" }],
  settings_fields: [
    { key: "search_quick_filters", label: "Quick filters", kind: "rows", pane: "general", section: "defaults",
      columns: [{ key: "label", label: "Label" }, { key: "query", label: "Query" }],
      value: [{ id: "1", label: "Untagged", query: "is:untagged" }], module_id: null },
    { key: "gpu_max_jobs", label: "GPU jobs (0 = auto)", kind: "number", pane: "general", section: "system", value: 0, help: "h", module_id: "threading" },
    { key: "min_free_gb", label: "Storage limit remaining (GB)", kind: "number", pane: "general", section: "system", value: 0, module_id: "fetch" }],
  missing_pip: [] };
const USER = { success: true, fields: [
  { key: "layout", label: "Layout", kind: "select", options: [{ value: "advanced", label: "Advanced" }, { value: "simple", label: "Simple" }],
    value: "simple", is_set: false, editable: true },
  { key: "search_quick_filters", label: "Search quick-filters", kind: "rows",
    columns: [{ key: "label", label: "Label" }, { key: "query", label: "Query" }],
    value: [{ id: "1", label: "Untagged", query: "is:untagged" }], is_set: false, editable: true }] };

async function boot(feats, admin) {
  const b = page();
  b.api.on("/api/modules", FIELDS);
  b.api.on("/api/user/settings", c => c.method === "POST" ? { success: true, fields: USER.fields } : USER);
  b.api.on("/api/auth/users", { users: [], groups: [], account_fields: [], mode: "local" });
  b.api.on("/api/auth/groups", { groups: [], account_fields: [], catalog: { sections: [], roles: [] } });
  b.api.on("/api/auth/features", { sections: [], roles: [] });
  await b.tick(20);     // let the boot-time /api/auth/me settle before overriding the user
  b.run(`CIMAuth.user = {username:"u", is_admin:${!!admin}, features:${JSON.stringify(feats || {})}};`);
  return b;
}
const $ = (b, sel) => b.document.querySelector(sel);
const visible = (b, sel) => { const e = $(b, sel); return !!e && !e.classList.contains("cim-feature-hidden") && !e.closest(".hidden"); };

test("viewer-like user: only User settings + Info; general falls back to user", async () => {
  const b = await boot({ "settings.general": 0, "settings.media": 0, "settings.storage": 0, "settings.models": 0,
                   "settings.users": 0, "settings.modules": 0, "settings.info": 1, "settings.pipeline": 0 });
  await b.tick(20);
  await b.run(`openSettings('general')`);
  await b.tick(20);
  assert.equal(b.run("window._settingsActiveTab"), "user");
  assert.ok(visible(b, '[data-settings-tab="user"]'));
  assert.ok(visible(b, '[data-settings-tab="info"]'));
  for (const t of ["general", "media", "storage", "models", "users", "modules", "module_pipeline"])
    assert.ok(!visible(b, `[data-settings-tab="${t}"]`), t);
  assert.ok($(b, '.settings-group[data-group="admin"]').classList.contains("hidden"));
  assert.ok($(b, '.settings-group[data-group="modules"]').classList.contains("hidden"));
});

test("read-only General: inputs read-only, buttons hidden, nothing saved for it", async () => {
  const b = await boot({ "settings.general": 1, "branding": 0 });
  await b.tick(20);
  await b.run(`openSettings('general')`);
  await b.tick(20);
  const pane = $(b, '[data-settings-pane="general"]');
  assert.ok(!pane.classList.contains("hidden"));
  assert.ok(pane.classList.contains("cim-read-only"));
  const num = pane.querySelector('#module_settings_fields_general_system input');
  assert.ok(num.readOnly);
  const add = [...pane.querySelectorAll("button")].find(x => x.textContent === "+ Add");
  assert.ok(add.classList.contains("cim-feature-hidden"));
  await b.run(`saveAllSettings()`);
  assert.equal(b.api.find("/api/update_settings", "POST").length, 0);
});

test("writable General: system strip is compact, rows editor saves through update_settings", async () => {
  const b = await boot({ "settings.general": 2 });
  await b.tick(20);
  await b.run(`openSettings('general')`);
  await b.tick(20);
  const strip = $(b, "#module_settings_fields_general_system");
  assert.equal(strip.querySelectorAll(".settings-compact-field").length, 2);
  assert.equal(strip.querySelector(".settings-compact-field").title, "h");
  const rows = $(b, "#module_settings_fields_general_defaults .settings-rows");
  assert.ok(rows);
  const inp = rows.querySelector('input[data-col="label"]');
  assert.ok(!inp.readOnly);
  inp.value = "Fresh"; inp.dispatchEvent(new b.window.Event("input"));
  await b.run(`saveAllSettings()`);
  const post = b.api.last("/api/update_settings", "POST");
  assert.deepEqual(post.body.search_quick_filters.map(r => r.label), ["Fresh"]);
});

test("rail groups collapse and remember, the active tab's group stays open", async () => {
  const b = await boot({}, true);
  await b.tick(20);
  await b.run(`openSettings('general')`);
  await b.tick(20);
  const head = $(b, '.settings-group[data-group="admin"] .settings-group-head');
  head.click();
  assert.ok($(b, '.settings-group[data-group="admin"] .settings-group-body').classList.contains("hidden"));
  assert.ok(b.run("localStorage.getItem('cim.settings.collapsed')").includes("admin"));
  b.run(`settingsTab('users')`);
  assert.ok(!$(b, '.settings-group[data-group="admin"] .settings-group-body').classList.contains("hidden"));
  // module tab lands in the Modules group with its permission
  const mt = $(b, '[data-settings-tab="module_pipeline"]');
  assert.equal(mt.closest(".settings-group").dataset.group, "modules");
  assert.equal(mt.getAttribute("data-feature"), "settings.pipeline");
});

test("User settings: edits save to /api/user/settings and fire cim:user-settings", async () => {
  const b = await boot({ "settings.general": 0 });
  await b.tick(20);
  await b.run(`openSettings('user')`);
  await b.tick(30);
  const sel = $(b, '#user_settings_fields .user-setting[data-key="layout"] select');
  assert.equal(sel.value, "simple");
  sel.value = "advanced"; sel.dispatchEvent(new b.window.Event("change"));
  let keys = null;
  b.window.addEventListener("cim:user-settings", e => { keys = e.detail.keys; });
  await b.run(`saveAllSettings()`);
  assert.deepEqual(b.api.last("/api/user/settings", "POST").body, { layout: "advanced" });
  assert.deepEqual([...keys], ["layout"]);
  assert.equal(b.api.find("/api/update_settings", "POST").length, 0);
});

test("User settings: a setting the user may not change is read-only", async () => {
  const b = await boot({});
  b.api.on("/api/user/settings", { success: true, fields: [Object.assign({}, USER.fields[0], { editable: false })] });
  await b.tick(20);
  await b.run(`openSettings('user')`);
  await b.tick(30);
  const sel = $(b, '#user_settings_fields select');
  assert.ok(sel.disabled);
  assert.match($(b, "#user_settings_fields").textContent, /Set by your administrator/);
});