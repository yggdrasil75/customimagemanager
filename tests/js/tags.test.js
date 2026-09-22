const { page, test, assert } = require("./_setup");
const b = page();
const $ = id => b.document.getElementById(id);

test("tagName / tagIsConfirmed mirror the Python sentinel rules", () => {
  assert.equal(b.run(`tagName("?cat")`), "cat");
  assert.equal(b.run(`tagName("cat")`), "cat");
  assert.equal(b.run(`tagIsConfirmed("cat")`), true);
  assert.equal(b.run(`tagIsConfirmed("?cat")`), false);
});

test("renderTags draws rows, counts, confirm button", () => {
  b.run(`window.currentFile="x.jxl"; currentRegions=[]; setTags(["cat","?dog"]); renderTags();`);
  const rows = $("tag_list").querySelectorAll(".tag-row");
  assert.equal(rows.length, 2);
  assert.ok(rows[1].classList.contains("tag-unconfirmed"));
  assert.match($("tag_count").textContent, /2 image/);
  assert.match($("tag_count").textContent, /1 unconfirmed/);
  assert.equal($("btn_confirm_all_tags").style.display, "inline-block");
});

test("acceptTag / rejectTag / removeTag / renameTag mutate currentTags", () => {
  b.run(`setTags(["cat","?dog","?bird"]); acceptTag(1);`);
  assert.deepEqual(b.val("currentTags"), ["cat", "dog", "?bird"]);
  b.run(`removeTag(2)`);
  assert.deepEqual(b.val("currentTags"), ["cat", "dog"]);
  b.run(`renameTag(1,"  DOG2 ")`);
  assert.deepEqual(b.val("currentTags"), ["cat", "DOG2"]);
  b.run(`renameTag(1,"")`);                          // cleared → delete
  assert.deepEqual(b.val("currentTags"), ["cat"]);
  b.run(`setTags(["?cat","cat2"]); renameTag(1,"cat")`);   // merge: keeps the more-confirmed
  assert.deepEqual(b.val("currentTags"), ["cat"]);
});

test("confirmAllTags strips every sentinel", () => {
  b.run(`setTags(["?a","?b","c"]); confirmAllTags();`);
  assert.deepEqual(b.val("currentTags"), ["a", "b", "c"]);
});

test("addTagsFromInput splits, trims, dedupes", () => {
  b.run(`setTags(["cat"]); document.getElementById("tag_add_input").value=" dog, Cat ,, bird "; addTagsFromInput();`);
  const tags = b.val("currentTags").map(t => t.toLowerCase());
  assert.deepEqual([...new Set(tags)].sort(), ["bird", "cat", "dog"]);
});

test("autosave POSTs the write packet once, debounced", async () => {
  b.api.reset();
  b.run(`window.currentFile="x.jxl"; currentRegions=[]; setTags(["cat"]); document.getElementById("meta_desc").value="d";
         triggerAutosave(); triggerAutosave();`);
  assert.equal($("save_indicator").innerText, "Saving…");
  await b.tick(1000);
  const w = b.api.find("/api/metadata", "POST");
  assert.equal(w.length, 1);
  assert.equal(w[0].body.action, "write");
  assert.equal(w[0].body.filename, "x.jxl");
  assert.deepEqual(w[0].body.tags, ["cat"]);
  assert.equal(w[0].body.description, "d");
  assert.equal($("save_indicator").innerText, "✓ Saved");
});
