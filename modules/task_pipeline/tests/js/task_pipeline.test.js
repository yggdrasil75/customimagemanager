// Frontend test for the task pipelines on the pipeline graph editor
// (modules/task_pipeline/static/task_pipeline.js): each pipeline is a graph source in the
// "Task pipelines" group, its panel lists the steps that can't run with the reason, and
// Save posts the task_pipelines setting with only this graph changed.
const { page, test, assert, skipUnless } = require("cim");
const skip = skipUnless("task_pipeline");

const GRAPH = { schema: "task/1", nodes: [
  { id: "start", type: "start" },
  { id: "llm", type: "s.llm", label: "LLM", enabled: false, in: { items: ["start", "items"] } },
  { id: "end", type: "end", in: { items: [["llm", "items"]] } }] };
const CATALOG = { "s.llm": { label: "LLM", note: "Can't run here: no LLM module is on - the run skips it.",
                             inputs: [], outputs: [], params: {} } };

async function open() {
  const b = page({ modules: ["task_pipeline"] });
  b.api.on("/api/task_pipelines/graph", { success: true, id: "p", graph: GRAPH, catalog: CATALOG,
                                         customised: true, saved: { p: GRAPH, other: { schema: "task/1", nodes: [] } } });
  await b.tick(20);
  b.run(`window._src = TaskPipelines.source({ id: "p", label: "P", summary: "What P does." })`);
  return b;
}

test("a task pipeline is a graph source whose panel says why a step can't run", { skip }, async () => {
  const b = await open();
  assert.deepEqual(b.errors, []);
  assert.equal(b.run("_src.id"), "task:p");
  assert.equal(b.run("_src.group"), "Task pipelines");
  b.run("_src.load().then(t => { window._loaded = t; })");
  await b.tick(20);
  assert.equal(b.val("_loaded.graph").schema, "task/1");
  b.run(`(()=>{ const el=document.createElement('div'); el.id='tp_panel'; document.body.appendChild(el);
               _src.panel(el, null); })()`);
  const text = b.document.getElementById("tp_panel").textContent;
  assert.match(text, /What P does\./);
  assert.match(text, /LLM: Can't run here: no LLM module is on/);
  assert.match(text, /Reset to default/);
});

test("saving a task graph posts the setting with only that graph changed", { skip }, async () => {
  const b = await open();
  b.run("_src.load().then(() => _src.save({ schema: 'task/1', nodes: [{ id: 'start', type: 'start' }] }))" +
        ".then(r => { window._saved = r; })");
  await b.tick(20);
  assert.equal(b.val("_saved").ok, true);
  const p = b.api.last("/api/update_settings", "POST");
  assert.ok(p, "posted through /api/update_settings");
  assert.deepEqual(Object.keys(p.body.task_pipelines).sort(), ["other", "p"]);
  assert.equal(p.body.task_pipelines.p.nodes.length, 1);
});