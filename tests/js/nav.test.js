// Viewer navigation (window.CIMNav: arrows, Home / End, swipe) and the
// gallery toolbar's folder breadcrumb + tree popover.
const { page, test, assert } = require("cim");
const b = page();
const $ = id => b.document.getElementById(id);

const img = f => ({ kind: "image", filename: f, tags: [], description: "", width: 4, height: 4 });
// Three pages of two files: p0 = a,b  p1 = c,d  p2 = e,f
const PAGES = [["a.jxl", "b.jxl"], ["c.jxl", "d.jxl"], ["e.jxl", "f.jxl"]];
function pagedList() {
  b.api.on("/api/list", c => ({ success: true, files: PAGES[+c.query.page || 0].map(img), total: 6, page: +c.query.page, page_size: 2 }));
}
const key = (k, target) => (target || b.document).dispatchEvent(new b.window.KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true }));
const cur = () => b.run("window.currentFile");

test("ArrowRight / ArrowLeft move through the page and across page edges", async () => {
  pagedList();
  b.run(`PAGE=2; galleryView='grid'; currentFolder=''; currentSearch=''; currentPage=0;`);
  await b.run("loadGallery()");
  b.run(`window.currentFile='a.jxl'`);
  key("ArrowRight"); await b.tick(20);
  assert.equal(cur(), "b.jxl");
  key("ArrowRight"); await b.tick(30);           // page edge: loads page 1, opens its first file
  assert.equal(b.run("currentPage"), 1);
  assert.equal(b.api.last("/api/list").query.page, "1");
  assert.equal(cur(), "c.jxl");
  key("ArrowLeft"); await b.tick(30);            // back over the edge: page 0, last file
  assert.equal(b.run("currentPage"), 0);
  assert.equal(cur(), "b.jxl");
  key("End"); await b.tick(30);
  assert.equal(b.run("currentPage"), 2);
  assert.equal(cur(), "f.jxl");
  assert.equal(b.run("CIMNav.hasNext()"), false);
  key("ArrowRight"); await b.tick(30);           // last file of the last page: stays
  assert.equal(cur(), "f.jxl");
  key("Home"); await b.tick(30);
  assert.equal(b.run("currentPage"), 0);
  assert.equal(cur(), "a.jxl");
  assert.equal(b.run("CIMNav.hasPrev()"), false);
});

test("arrows are ignored while typing or while a modal is open", async () => {
  pagedList();
  b.run(`PAGE=2; currentPage=0;`);
  await b.run("loadGallery()");
  b.run(`window.currentFile='a.jxl'`);
  const si = $("search_input");
  si.focus();
  key("ArrowRight", si); await b.tick(20);
  assert.equal(cur(), "a.jxl");
  si.blur();
  $("settings_modal").classList.remove("hidden");
  key("ArrowRight"); await b.tick(20);
  assert.equal(cur(), "a.jxl");
  $("settings_modal").classList.add("hidden");
  key("ArrowRight"); await b.tick(20);
  assert.equal(cur(), "b.jxl");
});

test("CIMNav.next / prev are exposed for modules", async () => {
  pagedList();
  b.run(`PAGE=2; currentPage=0;`);
  await b.run("loadGallery()");
  b.run(`window.currentFile='a.jxl'`);
  await b.run("CIMNav.next()");
  assert.equal(cur(), "b.jxl");
  await b.run("CIMNav.prev()");
  assert.equal(cur(), "a.jxl");
  for (const k of ["next", "prev", "first", "last"]) assert.equal(b.run(`typeof CIMNav.${k}`), "function");
});

test("swipe: long horizontal touch gestures navigate, short / vertical / mouse ones don't", async () => {
  assert.equal(b.run("CIMNav.swipeDir(-80, 10)"), 1);
  assert.equal(b.run("CIMNav.swipeDir(80, 10)"), -1);
  assert.equal(b.run("CIMNav.swipeDir(-40, 0)"), 0);
  assert.equal(b.run("CIMNav.swipeDir(-80, 60)"), 0);
  pagedList();
  b.run(`PAGE=2; currentPage=0;`);
  await b.run("loadGallery()");
  b.run(`window.currentFile='a.jxl'`);
  const cc = $("canvas_container");
  const W = b.window;
  const ptr = (type, x, y, pointerType = "touch") => {
    const Ev = W.PointerEvent || W.MouseEvent;
    const e = new Ev(type, { bubbles: true, cancelable: true, clientX: x, clientY: y, pointerId: 1, isPrimary: true, pointerType });
    if (!W.PointerEvent) Object.defineProperties(e, {   // jsdom without PointerEvent
      pointerType: { value: pointerType }, pointerId: { value: 1 }, isPrimary: { value: true } });
    cc.dispatchEvent(e);
  };
  const swipe = (x0, x1, dy = 0, pt) => { ptr("pointerdown", x0, 100, pt); ptr("pointerup", x1, 100 + dy, pt); };
  swipe(300, 200); await b.tick(20);              // finger left -> next
  assert.equal(cur(), "b.jxl");
  swipe(200, 300); await b.tick(20);              // finger right -> prev
  assert.equal(cur(), "a.jxl");
  swipe(300, 270); await b.tick(20);              // too short
  swipe(300, 200, 90); await b.tick(20);          // too vertical
  swipe(300, 200, 0, "mouse"); await b.tick(20);  // a mouse drag draws boxes, never swipes
  assert.equal(cur(), "a.jxl");
  b.run("window.__blockSwipe=true; CIMNav.addSwipeBlocker(()=>window.__blockSwipe)");
  swipe(300, 200); await b.tick(20);              // a box / crop tool is active
  assert.equal(cur(), "a.jxl");
  b.run("window.__blockSwipe=false");
  swipe(300, 200); await b.tick(20);
  assert.equal(cur(), "b.jxl");
});

const TREE = { name: "", path: "", count: 2, total: 9, children: [
  { name: "2026", path: "2026", count: 1, total: 6, children: [
    { name: "trip", path: "2026/trip", count: 5, total: 5, children: [] }] },
  { name: "scans", path: "scans", count: 1, total: 1, children: [] }] };

test("folder tree renders nested nodes with counts and picks a folder", async () => {
  b.api.on("/api/folders", { success: true, folders: [], tree: TREE });
  b.api.on("/api/list", { success: true, files: [], total: 0, page: 0, page_size: 200 });
  b.run(`currentFolder=''; currentFolderRecursive=false;`);
  await b.run("loadFolders()");
  assert.equal(b.api.last("/api/folders").query.tree, "1");
  b.run("toggleFolderTree(true)");
  assert.equal($("folder_tree_pop").classList.contains("hidden"), false);
  const paths = () => [...$("folder_tree").querySelectorAll("[data-fpath]")].map(r => r.dataset.fpath);
  assert.deepEqual(paths(), ["", "/", "2026", "scans"]);   // collapsed: trip hidden
  const row2026 = $("folder_tree").querySelector('[data-fpath="2026"]');
  assert.match(row2026.textContent, /1 \/ 6/);
  row2026.querySelector("[data-ftoggle]").click();          // expand
  assert.deepEqual(paths(), ["", "/", "2026", "2026/trip", "scans"]);
  assert.equal($("folder_tree_pop").classList.contains("hidden"), false);
  $("folder_tree").querySelector('[data-fpath="2026/trip"]').click();
  await b.tick(10);
  assert.equal(b.run("currentFolder"), "2026/trip");
  assert.equal($("folder_select").value, "2026/trip");
  assert.equal(b.api.last("/api/list").query.folder, "2026/trip");
  assert.equal($("folder_tree_pop").classList.contains("hidden"), true);
});

test("tree filter narrows to matches and their ancestors", async () => {
  b.api.on("/api/folders", { success: true, folders: [], tree: TREE });
  await b.run("loadFolders()");
  b.run("toggleFolderTree(true)");
  const f = $("folder_tree_filter");
  f.value = "tri"; f.dispatchEvent(new b.window.Event("input"));
  const paths = [...$("folder_tree").querySelectorAll("[data-fpath]")].map(r => r.dataset.fpath);
  assert.deepEqual(paths, ["", "2026", "2026/trip"]);
  f.value = "zzz"; f.dispatchEvent(new b.window.Event("input"));
  assert.match($("folder_tree").textContent, /No folder matches/);
  f.value = ""; f.dispatchEvent(new b.window.Event("input"));
  b.run("toggleFolderTree(false)");
});

test("breadcrumb shows the path and its segments go up", async () => {
  b.api.on("/api/list", { success: true, files: [], total: 0, page: 0, page_size: 200 });
  b.run(`setFolder('2026/trip')`);
  await b.tick(10);
  const crumbs = () => [...$("folder_crumbs").querySelectorAll(".fcrumb")].map(x => x.textContent);
  assert.deepEqual(crumbs().slice(0, 3), ["All", "2026", "trip"]);
  $("folder_crumbs").querySelector('[data-crumb="2026"]').click();
  await b.tick(10);
  assert.equal(b.run("currentFolder"), "2026");
  assert.equal(b.api.last("/api/list").query.folder, "2026");
  $("folder_crumbs").querySelector('[data-crumb=""]').click();
  await b.tick(10);
  assert.equal(b.run("currentFolder"), "");
  assert.deepEqual(crumbs().slice(0, 1), ["All"]);
  b.run(`setFolder('a/b/c/d')`);
  await b.tick(10);
  assert.deepEqual(crumbs().slice(0, 4), ["All", "...", "c", "d"]);   // long paths collapse
});

test("include subfolders sends recursive=1 and shows in the breadcrumb", async () => {
  b.api.on("/api/list", { success: true, files: [], total: 0, page: 0, page_size: 200 });
  b.run(`setFolder('2026')`);
  await b.tick(10);
  assert.equal(b.api.last("/api/list").query.recursive, undefined);
  const rec = $("folder_recursive");
  rec.checked = true; rec.dispatchEvent(new b.window.Event("change"));
  await b.tick(10);
  assert.equal(b.api.last("/api/list").query.recursive, "1");
  assert.match($("folder_crumbs").textContent, /\+sub/);
  assert.equal(b.val("galleryQuery()").recursive, true);
  rec.checked = false; rec.dispatchEvent(new b.window.Event("change"));
  await b.tick(10);
  assert.equal(b.api.last("/api/list").query.recursive, undefined);
});
