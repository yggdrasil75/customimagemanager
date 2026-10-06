// Timeline view on the gallery's view switcher (registerGalleryView).
const { page, test, assert, skipUnless } = require("cim");
const skip = skipUnless("timeline");

const YEARS = { success: true, level: "year", total: 5, buckets: [
  { key: "2023", count: 1, samples: ["e.jxl"] },
  { key: "2021", count: 3, samples: ["c.jxl", "b.jxl", "a.jxl"] },
  { key: "undated", count: 1, samples: ["f.jxl"] }] };
const MONTHS = { success: true, level: "month", total: 5, buckets: [
  { key: "2023-07", count: 1, samples: ["e.jxl"] },
  { key: "2021-03", count: 3, samples: ["c.jxl", "b.jxl", "a.jxl"] },
  { key: "undated", count: 1, samples: ["f.jxl"] }] };
const FILES = {
  "2023-07": [{ filename: "e.jxl", kind: "image", date: "2023-07-15" }],
  "2021-03": [{ filename: "c.jxl", kind: "image", date: "2021-03-20" },
              { filename: "b.jxl", kind: "video", date: "2021-03-04" },
              { filename: "a.jxl", kind: "image", date: "2021-03-04" }],
  "undated": [{ filename: "f.jxl", kind: "image", date: null }],
};

async function boot() {
  const b = page({ modules: ["timeline"] });
  // Fire every observed section at once so day-level months load in jsdom.
  b.window.IntersectionObserver = class {
    constructor(cb) { this.cb = cb; }
    observe(el) { setTimeout(() => this.cb([{ isIntersecting: true, target: el }]), 0); }
    unobserve() { } disconnect() { }
  };
  b.api.on("/api/timeline/buckets", c => (c.query.level === "year" ? YEARS : MONTHS));
  b.api.on("/api/timeline/files", c => {
    const files = FILES[c.query.period] || [];
    return { success: true, files, total: files.length, offset: 0, limit: 2000 };
  });
  await b.tick(10);                       // module JS registers on DOMContentLoaded
  return b;
}
const $ = (b, s) => b.document.querySelector(s);
const $$ = (b, s) => [...b.document.querySelectorAll(s)];

test("switcher shows the timeline button and the grid chrome steps aside", { skip }, async () => {
  const b = await boot();
  assert.deepEqual(b.errors, []);
  const sw = b.document.getElementById("gallery_view_switch");
  assert.ok(!sw.classList.contains("hidden"));
  assert.ok($(b, '[data-gview="timeline"]'));
  b.run(`setGalleryView('timeline')`);
  await b.tick(20);
  assert.ok(b.document.getElementById("gallery_scroll").classList.contains("view-hidden"));
  assert.ok(b.document.getElementById("gallery_pager_bar").classList.contains("view-hidden"));
  assert.ok(!b.document.getElementById("gallery_view_host").classList.contains("hidden"));
  assert.match(b.window.location.search, /view=timeline/);
  b.run(`setGalleryView('grid')`);
  await b.tick(10);
  assert.ok(!b.document.getElementById("gallery_scroll").classList.contains("view-hidden"));
  assert.equal(b.document.getElementById("gallery_view_host").innerHTML, "");
});

test("years -> months -> days, with day groups, undated last", { skip }, async () => {
  const b = await boot();
  b.run(`setGalleryView('timeline')`);
  await b.tick(20);
  const cards = $$(b, ".tl-years .tl-card");
  assert.deepEqual(cards.map(c => c.dataset.key), ["2023", "2021", "undated"]);
  assert.equal(cards[1].querySelectorAll("img").length, 3);
  assert.equal(b.api.last("/api/timeline/buckets").query.samples, "9");

  cards[1].click();                                      // zoom into 2021
  await b.tick(20);
  assert.equal(b.api.last("/api/timeline/buckets").query.level, "month");
  assert.deepEqual($$(b, ".tl-section").map(s => s.dataset.key), ["2023", "2021", "undated"]);
  assert.ok($(b, '[data-tl-level="months"]').classList.contains("on"));

  $(b, '.tl-card[data-key="2021-03"]').click();           // zoom into March 2021
  await b.tick(40);
  assert.deepEqual($$(b, ".tl-month").map(s => s.dataset.key), ["2023-07", "2021-03", "undated"]);
  const days = $$(b, '.tl-month[data-key="2021-03"] .tl-day').map(d => d.dataset.key);
  assert.deepEqual(days, ["2021-03-20", "2021-03-04"]);
  assert.equal($$(b, '.tl-day[data-key="2021-03-04"] .tl-tile').length, 2);
  assert.ok($(b, '.tl-tile[data-filename="b.jxl"] .tl-play'));
  assert.ok($(b, '.tl-month[data-key="undated"] .tl-day[data-key="undated"]'));
  assert.deepEqual([...b.val("galleryFiles")].map(f => f.filename),
                   ["e.jxl", "c.jxl", "b.jxl", "a.jxl", "f.jxl"]);
});

test("day select and ctrl-click feed the shared selection / bulk bar", { skip }, async () => {
  const b = await boot();
  b.run(`setGalleryView('timeline')`);
  await b.tick(20);
  b.run(`document.querySelector('[data-tl-level="days"]').click()`);
  await b.tick(40);
  $(b, '.tl-day[data-key="2021-03-04"] .tl-daysel').click();
  assert.deepEqual([...b.run("[...selectedFiles].sort()")], ["a.jxl", "b.jxl"]);
  assert.ok(!b.document.getElementById("bulk_bar").classList.contains("hidden"));
  assert.ok($(b, '.tl-tile[data-filename="a.jxl"]').classList.contains("multi-selected"));
  $(b, '.tl-day[data-key="2021-03-04"] .tl-daysel').click();
  assert.equal(b.run("selectedFiles.size"), 0);
  const t = $(b, '.tl-tile[data-filename="c.jxl"]');
  t.dispatchEvent(new b.window.MouseEvent("click", { bubbles: true, ctrlKey: true }));
  assert.deepEqual([...b.run("[...selectedFiles]")], ["c.jxl"]);
  b.run(`clearSelection()`);
});

test("a new search re-queries the timeline instead of the grid", { skip }, async () => {
  const b = await boot();
  b.run(`setGalleryView('timeline')`);
  await b.tick(20);
  const before = b.api.find("/api/list").length;
  b.run(`currentSearch="tag:dog"; loadGallery();`);
  await b.tick(20);
  assert.equal(b.api.find("/api/list").length, before);
  assert.equal(b.api.last("/api/timeline/buckets").query.q, "tag:dog");
  b.run(`currentSearch=""; setGalleryView('grid')`);
});

test("server errors show in the view", { skip }, async () => {
  const b = await boot();
  b.api.on("/api/timeline/buckets", { success: false, error: "The timeline can't show a semantic search" });
  b.run(`setGalleryView('timeline')`);
  await b.tick(20);
  assert.match($(b, ".tl-empty").textContent, /semantic/);
});