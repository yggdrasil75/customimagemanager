// The left tab bar's overflow (static/panes.js): tabs that don't fit the bar's
// width, and every "tools" tab, go under the More menu; the active one shows
// on the button. jsdom has no layout, so widths are stubbed per button.
const { page, test, assert } = require("cim");

function open(widths, barWidth) {
  const b = page({ modules: [] });
  const bar = b.document.getElementById("left_tabs_bar");
  Object.defineProperty(bar, "clientWidth", { get: () => barWidth, configurable: true });
  for (const [id, w] of Object.entries(widths)) {
    const el = b.document.getElementById(id);
    el.getBoundingClientRect = () => ({ width: w });
  }
  b.document.getElementById("left_tabs_more").getBoundingClientRect = () => ({ width: 60 });
  // the registered tabs' buttons are made later; stub as they appear
  b.run(`
    for (const [id, w] of [["music", 80], ["books", 80], ["map", 60]])
      registerLeftTab({ id, label: id[0].toUpperCase() + id.slice(1), paneId: id + "_pane" });
    registerLeftTab({ id: "trainer", label: "Trainer", group: "tools" });
    registerLeftTab({ id: "iqa_train", label: "IQA train", group: "tools" });
  `);
  for (const [id, w] of [["music", 80], ["books", 80], ["map", 60], ["trainer", 80], ["iqa_train", 90]])
    b.document.querySelector(`[data-ltab="${id}"]`).getBoundingClientRect = () => ({ width: w });
  for (const id of ["music_pane", "books_pane", "map_pane", "trainer_pane", "iqa_train_pane"]) {
    const d = b.document.createElement("div"); d.id = id; d.className = "hidden"; b.document.body.appendChild(d);
  }
  return { b, bar };
}
const shown = (b, sel) => b.document.querySelector(sel).style.display !== "none";
const items = b => [...b.document.querySelectorAll("#left_tabs_more_menu [data-ltab-item]")].map(x => x.dataset.ltabItem);

test("tools tabs always sit under More; the rest fit the bar", async () => {
  const { b } = open({ tab_gallery: 90, tab_albums: 90, tab_review: 90 }, 1000);
  await b.run("_layoutLeftTabs()");
  assert.ok(shown(b, "#tab_gallery") && shown(b, '[data-ltab="music"]') && shown(b, '[data-ltab="map"]'));
  assert.ok(!shown(b, '[data-ltab="trainer"]') && !shown(b, '[data-ltab="iqa_train"]'));
  assert.ok(shown(b, "#left_tabs_more"));
  assert.deepEqual(items(b), ["trainer", "iqa_train"]);
});

test("a narrow bar moves the last tabs under More in order", async () => {
  // 90*3 + 80 + 80 + 60 = 490 > 420: with More's 60 only gallery, albums, review, music fit (350 + 60)
  const { b } = open({ tab_gallery: 90, tab_albums: 90, tab_review: 90 }, 420);
  await b.run("_layoutLeftTabs()");
  assert.ok(shown(b, "#tab_gallery") && shown(b, "#tab_review") && shown(b, '[data-ltab="music"]'));
  assert.ok(!shown(b, '[data-ltab="books"]') && !shown(b, '[data-ltab="map"]'));
  assert.deepEqual(items(b), ["books", "map", "trainer", "iqa_train"]);
  // a menu item opens the tab and the More button takes its label and the active style
  b.document.getElementById("left_tabs_more_btn").click();
  assert.ok(!b.document.getElementById("left_tabs_more_menu").classList.contains("hidden"));
  b.document.querySelector('[data-ltab-item="books"]').click();
  await b.tick(5);
  assert.equal(b.run("currentPane"), "books");
  assert.ok(b.document.getElementById("left_tabs_more_menu").classList.contains("hidden"), "menu closed");
  const btn = b.document.getElementById("left_tabs_more_btn");
  assert.ok(btn.textContent.startsWith("Books") && btn.className.includes("text-blue-400"));
  assert.ok(!b.document.getElementById("books_pane").classList.contains("hidden"));
  b.run("setPane('gallery')");   // (the built-in button's inline onclick does not run in jsdom)
  await b.tick(5);
  assert.ok(btn.textContent.startsWith("More") && !btn.className.includes("text-blue-400"));
});

test("a feature-hidden tab takes no slot and a wide bar needs no More", async () => {
  const { b } = open({ tab_gallery: 90, tab_albums: 90, tab_review: 90 }, 10000);
  b.run("unregisterLeftTab('trainer'); unregisterLeftTab('iqa_train')");
  b.document.querySelector('[data-ltab="map"]').classList.add("cim-feature-hidden");
  await b.run("_layoutLeftTabs()");
  assert.ok(!shown(b, "#left_tabs_more"));
  assert.deepEqual(items(b), []);
});