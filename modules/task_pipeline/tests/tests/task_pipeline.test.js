// Frontend test for the Settings > Task pipelines editor (modules/task_pipeline/static/task_pipeline.js):
// it opens with number and select options and shows their values.
const { page, test, assert, skipUnless } = require("cim");
const skip = skipUnless("task_pipeline");

test("task pipelines open with numeric option values", { skip }, async () => {
  const b = page({ modules: ["task_pipeline"] });
  assert.deepEqual(b.errors, []);
  b.api.on("/api/task_pipelines", { success: true, customised: [], pipelines: [{
    id: "p", label: "P", summary: "", steps: [{
      step: "s", label: "S", summary: "", enabled: true, locked: false,
      option_defs: [{ key: "n", kind: "number", label: "N" },
                    { key: "c", kind: "select", label: "C", choices: [{ value: 2, label: 2 }, { value: 3, label: "three" }] }],
      options: { n: 0.5, c: 3 } }] }] });
  b.run(`(()=>{ const d=document.createElement('div'); d.id='settings_pane_module_task_pipelines';
               document.body.appendChild(d); TaskPipelines.reload(); })()`);
  await b.tick(20);
  assert.deepEqual(b.errors, []);
  const root = b.document.getElementById("tp_root");
  assert.ok(root, "the editor rendered");
  assert.equal(root.querySelector('input[type="number"]').value, "0.5");
  assert.equal(root.querySelector("select").value, "3");
});