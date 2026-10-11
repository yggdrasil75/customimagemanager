// Frontend test for modules/library_pipeline: the Library graph source registers with
// the graph editor, and the plan panel previews, lists and applies a plan.
const { page, test, assert, skipUnless } = require("cim");
const skip = skipUnless("library_pipeline");

const PLAN = { id: 7, status: "planned", finished: 1717400000, log: ["Trips: 1 groups"], applied: {},
  plan: [{ id: 0, op: "album", name: "Lisbon with Ana", album: "Lisbon with Ana", description: "A long weekend.",
           files: ["a.jpg", "b.jpg"], samples: ["a.jpg", "b.jpg"],
           facts: { count: 2, start: 1717400000, end: 1717500000, days: 2, cities: ["Lisbon"], countries: ["Portugal"], people: ["Ana"] } },
         { id: 1, op: "move", name: "Lisbon with Ana", folder: "2024/Lisbon", files: ["a.jpg"], samples: ["a.jpg"],
           blocked: "moving files is off", facts: { count: 1 } }] };

test("registers the Library graph and the plan panel previews and applies", { skip }, async () => {
  const b = page({ modules: ["library_pipeline"] });
  assert.deepEqual(b.errors, []);
  const LP = b.window.CIMLibraryPipeline;
  assert.ok(LP && LP.source.id === "library", "source exposed");
  const queue = b.window.CIMPipelineGraphSources;
  assert.ok(queue, "source queued for the graph editor");

  b.api.on("/api/library_pipeline/status", { success: true, llm: true, auto: "off", allow_moves: false,
    state: { running: false, run_id: 7, phase: "", progress: "" }, run: PLAN });
  b.api.on("POST /api/library_pipeline/run", { success: true, run_id: 7 });
  b.api.on("POST /api/library_pipeline/apply", { success: true, run_id: 7 });
  b.window.confirm = () => true;

  const el = b.document.createElement("div");
  b.document.body.appendChild(el);
  LP.source.panel(el, null);
  await b.tick(20);
  const text = el.textContent;
  assert.match(text, /Lisbon with Ana/);
  assert.match(text, /A long weekend\./);
  assert.match(text, /blocked/);
  assert.equal(el.querySelectorAll("[data-lp-pick]").length, 1, "a blocked entry can't be ticked");
  assert.equal(el.querySelectorAll(".lp-thumbs img").length, 3);

  el.querySelector("#lp_preview").click();
  await b.tick(20);
  assert.ok(b.api.last("/api/library_pipeline/run", "POST"), "preview posts a run");

  const cb = el.querySelector("[data-lp-pick]");
  cb.checked = true;
  cb.dispatchEvent(new b.window.Event("change", { bubbles: true }));
  el.querySelector("#lp_apply").click();
  await b.tick(20);
  const call = b.api.last("/api/library_pipeline/apply", "POST");
  assert.ok(call, "apply posted");
  assert.deepEqual(JSON.parse(JSON.stringify(call.body)), { run_id: 7, ids: [0] });
});

test("Review and bulk-bar buttons run it; a pending plan is a Review section", { skip }, async () => {
  const b = page({ modules: ["library_pipeline"] });
  await b.tick(20);
  const btns = b.window._extButtons || {};
  assert.ok((btns.review_actions || []).some(h => /lp_btn_review/.test(h)), "Review action");
  assert.ok((btns.gallery_bulk || []).some(h => /lp_btn_bulk/.test(h)), "bulk-bar action");
  b.api.on("POST /api/library_pipeline/run", { success: true, run_id: 7 });
  b.api.on("/api/library_pipeline/status", { success: true, llm: false, auto: "off", allow_moves: false,
    state: { running: false, run_id: 7 }, run: PLAN });
  b.run("selectedFiles.clear(); selectedFiles.add('a.jpg'); selectedFiles.add('b.jpg');");
  await b.window.CIMLibraryPipeline.runSelection();
  const call = b.api.last("/api/library_pipeline/run", "POST");
  assert.deepEqual(JSON.parse(JSON.stringify(call.body)), { files: ["a.jpg", "b.jpg"] });
  const box = b.document.createElement("section");
  b.document.body.appendChild(box);
  const n = await b.window.CIMLibraryPipeline.reviewSection(box);
  assert.equal(n, 1, "one entry left to apply (the move is blocked)");
  assert.match(box.textContent, /Library pipeline \| 1 proposed/);
});