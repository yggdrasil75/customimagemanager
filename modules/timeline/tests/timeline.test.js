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
/** @brief Boot, open the timeline and go to the days view. */
async function days(b) {
  b.run(`setGalleryView('timeline')`);
  await b.tick(20);
  b.run(`document.querySelector('[data-tl-level="days"]').click()`);
  await b.tick(40);
}
const crumb = b => $(b, "#tl_crumb").textContent;

test("days scrubber: ticks spaced by count, drag jumps and labels the month", { skip }, async () => {
  const b = await boot();
  await days(b);
  assert.ok($(b, "#tl_rail").classList.contains("tl-rail-scrub"));
  const ticks = $$(b, ".tl-scrub-track .tl-tick");
  // 1 + 3 + 1 files: segments start at 0%, 20%, 80%
  assert.deepEqual(ticks.map(t => t.dataset.tick), ["2023-07", "2021-03", "undated"]);
  assert.deepEqual(ticks.map(t => parseFloat(t.style.top)), [0, 20, 80]);
  assert.deepEqual(ticks.map(t => t.textContent), ["2023", "2021", "-"]);
  const track = $(b, ".tl-scrub-track");
  track.getBoundingClientRect = () => ({ top: 0, bottom: 100, height: 100, left: 0, right: 40, width: 40 });
  track.dispatchEvent(new b.window.MouseEvent("pointerdown", { bubbles: true, clientY: 50, button: 0 }));
  const lab = $(b, ".tl-scrub-label");
  assert.equal(track.dataset.at, "2021-03");
  assert.ok(!lab.classList.contains("hidden"));
  assert.match(lab.textContent, /2021/);
  assert.match(crumb(b), /2021/);
  b.window.dispatchEvent(new b.window.MouseEvent("pointermove", { clientY: 95 }));
  assert.equal(track.dataset.at, "undated");
  assert.equal(lab.textContent, "Undated");
  b.window.dispatchEvent(new b.window.MouseEvent("pointerup", { clientY: 95 }));
  assert.ok(lab.classList.contains("hidden"));
  b.window.dispatchEvent(new b.window.MouseEvent("pointermove", { clientY: 5 }));
  assert.equal(track.dataset.at, "undated");                 // released: no more jumps
  // years view keeps the plain year rail
  b.run(`document.querySelector('[data-tl-level="years"]').click()`);
  await b.tick(20);
  assert.ok(!$(b, "#tl_rail").classList.contains("tl-rail-scrub"));
  assert.ok($(b, '#tl_rail [data-year="2021"]'));
});

test("month select toggles every file of the month, loaded or not", { skip }, async () => {
  const b = await boot();
  await days(b);
  $(b, '.tl-monthsel[data-month="2021-03"]').click();
  await b.tick(10);
  assert.deepEqual([...b.run("[...selectedFiles].sort()")], ["a.jxl", "b.jxl", "c.jxl"]);
  $(b, '.tl-monthsel[data-month="2021-03"]').click();
  await b.tick(10);
  assert.equal(b.run("selectedFiles.size"), 0);
  // a month whose section never loaded is fetched first
  b.window.IntersectionObserver = class { observe() { } unobserve() { } disconnect() { } };
  b.run(`document.querySelector('[data-tl-level="months"]').click()`);
  await b.tick(20);
  b.run(`document.querySelector('[data-tl-level="days"]').click()`);
  await b.tick(20);
  assert.equal($$(b, ".tl-tile").length, 0);
  $(b, '.tl-monthsel[data-month="2023-07"]').click();
  await b.tick(20);
  assert.deepEqual([...b.run("[...selectedFiles]")], ["e.jxl"]);
  assert.equal(b.api.last("/api/timeline/files").query.period, "2023-07");
  b.run(`clearSelection()`);
});

test("PageDown / PageUp jump a month in the days view", { skip }, async () => {
  const b = await boot();
  await days(b);
  const key = k => b.document.dispatchEvent(new b.window.KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true }));
  key("PageDown");
  assert.match(crumb(b), /2021/);
  key("PageDown");
  assert.match(crumb(b), /Undated/);
  key("PageDown");                                         // stays on the last month
  assert.match(crumb(b), /Undated/);
  key("PageUp");
  assert.match(crumb(b), /2021/);
  key("PageUp");
  assert.match(crumb(b), /2023/);
});

test("'On this day' per day header only with the memories module", { skip }, async () => {
  const b = await boot();
  await days(b);
  assert.equal($$(b, ".tl-otd").length, 0);
  let opened = null;
  b.window.CIMMemories = { open: d => { opened = d; } };
  b.run(`document.querySelector('[data-tl-level="months"]').click()`);
  await b.tick(20);
  b.run(`document.querySelector('[data-tl-level="days"]').click()`);
  await b.tick(40);
  assert.ok($(b, '.tl-day[data-key="2021-03-04"] .tl-otd'));
  assert.ok(!$(b, '.tl-day[data-key="undated"] .tl-otd'));
  $(b, '.tl-day[data-key="2021-03-04"] .tl-otd').click();
  assert.equal(opened, "2021-03-04");
});
