// Frontend test for the per-photo pipelines panel (modules/pipeline/static/pipelines.js):
// it shows the scope, mode and agent memory, and saves through /api/pipelines/save.
const { page, test, assert, skipUnless } = require("cim");
const skip = skipUnless("pipeline");

const LIST = { success: true, modes: ["graph", "semi", "full"], pipelines: [
  { id: "family_trips", name: "Family trips", q: "person:ana", folder: "", album: "", enabled: true, mode: "semi",
    goal: "Name the family.", max_steps: 6, agent_runs: 2 },
  { id: "default", name: "Default", q: "", folder: "", album: "", enabled: true, mode: "graph", goal: "", max_steps: 8 }] };

test("pipeline panel: scope, agent memory, save", { skip }, async () => {
  const b = page({ modules: ["pipeline"] });
  assert.deepEqual(b.errors, []);
  b.api.on("/api/pipelines", LIST);
  b.api.on("/api/pipelines/match", { success: true, counts: { family_trips: 12, default: 40 } });
  b.api.on("/api/pipelines/agent", { success: true, runs: 2, last_run: { rel_path: "t/a.jpg", at: 1717400000 },
    last_history: [{ tool: "detect_persons", params: {}, why: "people first", result: "2 subjects" }],
    recurring: [{ tool: "ocr", label: "", runs: 2 }], learned_steps: [{ tool: "detect_persons" }, { tool: "ocr" }] });
  b.api.on("POST /api/pipelines/save", { success: true, pipelines: LIST.pipelines });
  const el = b.document.createElement("div");
  b.document.body.appendChild(el);
  await b.window.CIMPipelines.panel(el, null, "family_trips");
  await b.tick(30);
  assert.equal(el.querySelector("#plp_q").value, "person:ana");
  assert.equal(el.querySelector("#plp_mode").value, "semi");
  assert.ok(!el.querySelector("#plp_agent_opts").classList.contains("hidden"), "agent options shown in semi");
  assert.match(el.textContent, /Scope lists 12 file/);
  assert.match(el.textContent, /2 run\(s\)/);
  assert.match(el.textContent, /people first/);
  assert.match(el.textContent, /ocr x2/);
  el.querySelector("#plp_q").value = "person:ana tag:trip";
  el.querySelector("#plp_save").click();
  await b.tick(20);
  const call = b.api.last("/api/pipelines/save", "POST");
  assert.ok(call, "saved");
  const body = JSON.parse(JSON.stringify(call.body));
  assert.equal(body.old_id, "family_trips");
  assert.equal(body.pipeline.q, "person:ana tag:trip");
  assert.equal(body.pipeline.mode, "semi");
});

test("the default pipeline has no scope fields and can't be deleted", { skip }, async () => {
  const b = page({ modules: ["pipeline"] });
  b.api.on("/api/pipelines", LIST);
  b.api.on("/api/pipelines/match", { success: true, counts: {} });
  b.api.on("/api/pipelines/agent", { success: true, runs: 0 });
  const el = b.document.createElement("div");
  b.document.body.appendChild(el);
  await b.window.CIMPipelines.panel(el, null, "default");
  await b.tick(20);
  assert.equal(el.querySelector("#plp_q"), null);
  assert.equal(el.querySelector("#plp_delete"), null);
  assert.match(el.textContent, /no other pipeline above it takes/);
});