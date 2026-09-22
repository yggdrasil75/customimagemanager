const { page, test, assert } = require("cim");
const b = page();

test("rvDecide / rvAll / rvName build the decisions rvSave posts", async () => {
  b.run(`reviewItems=[{filename:"r.jxl",unconfirmed:2,flagged:false}]; reviewIdx=0;
         _rvRegions=[{class_name:"person",cx:.5,cy:.5,w:.4,h:.8},{class_name:"face",cx:.2,cy:.2,w:.1,h:.1},{class_name:"x",cx:.8,cy:.8,w:.1,h:.1}];
         _rvDecisions={}; rvAll("accept"); rvDecide(1,"deny"); rvDecide(2,"keep"); rvName(0,"girl");`);
  b.api.on("/api/review_boxes", { success: true, accepted: 1, denied: 1, remaining_unconfirmed: 0 });
  b.run(`rvSave(false)`);
  await b.tick(20);
  const call = b.api.last("/api/review_boxes", "POST");
  assert.equal(call.body.filename, "r.jxl");
  assert.deepEqual(call.body.decisions, [
    { index: 0, action: "accept", name: "girl" },
    { index: 1, action: "deny" },
    { index: 2, action: "rename", name: "x" }]);
  assert.equal(b.run("reviewItems[0].unconfirmed"), 0);
});

test("refreshReviewCount reflects the server total", async () => {
  b.api.on("/api/review_list", { success: true, total: 7, items: [], counts: { delete: 2, boxes: 5, tags: 0 } });
  b.run(`refreshReviewCount()`);
  await b.tick(20);
  const badge = b.document.getElementById("review_tab_badge");
  assert.match(badge.innerText || badge.textContent, /7/);
  b.api.on("/api/review_list", { success: true, total: 0, items: [], counts: {} });
  b.run(`refreshReviewCount()`);
  await b.tick(20);
  assert.equal((badge.innerText || badge.textContent), "");
});
