// Settings modal: opening it again rebuilds the module fields, but whatever a module
// rendered into a field mount itself (the pipeline graph editor) stays, and no field
// is drawn twice.
const { page, test, assert } = require("cim");

const MODULES = { modules: [], missing_pip: [],
  settings_tabs: [{ id: "pipeline", label: "Pipeline", icon: "", admin_only: true, group: "modules",
                    feature: "settings.pipeline", module_id: "pipeline" }],
  settings_fields: [
    { key: "pipe_knob", label: "Knob", kind: "number", pane: "pipeline", value: 1, module_id: "pipeline" },
    { key: "gpu_max_jobs", label: "GPU jobs", kind: "number", pane: "general", section: "system", value: 0, module_id: "threading" }] };

test("reopening Settings keeps module-rendered UI and doesn't duplicate fields", async () => {
  const b = page();
  b.api.on("/api/modules", MODULES);
  b.api.on("/api/user/settings", { success: true, fields: [] });
  await b.tick(20);
  b.run(`CIMAuth.user = {username: "u", is_admin: true, features: {}};`);
  await b.run(`openSettings('general')`);
  await b.tick(30);
  const mount = b.document.getElementById("module_settings_fields_pipeline");
  assert.ok(mount, "the module tab's field mount exists");
  const editor = b.document.createElement("div");
  editor.id = "my_module_editor";
  mount.prepend(editor);                       // what pipeline_graph.js does with its canvas
  for (let i = 0; i < 2; i++) {
    await b.run(`closeSettings()`);
    await b.run(`openSettings('general')`);
    await b.tick(30);
  }
  assert.ok(b.document.getElementById("my_module_editor"), "module UI survived two reopenings");
  assert.equal(b.document.querySelectorAll('[data-settings-field="pipe_knob"]').length, 1);
  assert.equal(b.document.querySelectorAll('[data-settings-field="gpu_max_jobs"]').length, 1);
});