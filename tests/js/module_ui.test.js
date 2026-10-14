// Declared module UI (static/module_ui.js): buttons, settings sections and tile
// badges a module describes in Python are drawn by the core and call the routes
// the host made for them.
const { page, test, assert } = require("cim");

const UI = {
  buttons: [
    { area: "ai_tools", id: "go", label: "Go", url: "/api/ui/t/button/go/run", needs: "file", feature: null,
      variant: "primary", title: "t", confirm: null, on_done: "apply", busy: "Going...", options: [], module_id: "t" },
    { area: "gallery_bulk", id: "exp", label: "Export", url: "/api/ui/t/button/exp/run", needs: "selection", feature: null,
      variant: "ok", title: null, confirm: null, on_done: null, busy: null,
      options: [{ value: "a", label: "A" }, { value: "b", label: "B" }], module_id: "t" },
  ],
  sections: [{ pane: "t", id: "s", title: "T", text: "hello", poll: null, order: 1, feature: null, mount: null,
    status_url: "/api/ui/t/section/s/status",
    actions: [{ id: "do", label: "Do", url: "/api/ui/t/section/s/action_do", variant: null, title: null, confirm: null }],
    module_id: "t" }],
  left_tabs: [], controls_tabs: [],
  tile_badges: [{ field: "fav", text: "FAV {value}", title: null, tone: "ok", feature: null, when: null, module_id: "t" }],
};

function boot() {
  const b = page({});
  b.api.on("/api/module_ui", UI);
  return b;
}

test("a declared file button posts the open file and applies the reply", async () => {
  const b = boot();
  await b.tick(30);
  const btn = b.document.getElementById("ui_btn_t_go");
  assert.ok(btn, "button injected into ai_tools");
  b.window.currentFile = "x.jxl";
  b.window.currentRegions = [];
  let applied = null;
  b.window.applyAiResult = d => { applied = d; };
  b.api.on("POST /api/ui/t/button/go/run", { success: true, regions: [{ cx: 0.5 }], message: "done" });
  b.window.cimUiRun("ui_btn_t_go", btn);          // what the button's onclick runs (jsdom runs no inline handlers)
  await b.tick(20);
  const call = b.api.last("/api/ui/t/button/go/run", "POST");
  assert.deepEqual(call.body, { filename: "x.jxl" });
  assert.deepEqual(applied.regions, [{ cx: 0.5 }]);
  assert.equal(btn.textContent, "Go");            // label restored after the run (jsdom has no innerText)
});

test("a selection button with options sends the pick and the selection", async () => {
  const b = boot();
  await b.tick(30);
  const btn = b.document.getElementById("ui_btn_t_exp");
  assert.ok(btn, "button injected into gallery_bulk");
  const sel = btn.parentElement.querySelector("select[data-ui-option]");
  assert.ok(sel && sel.options.length === 2);
  sel.value = "b";
  b.window.selectedFiles = new Set(["p.jxl", "q.jxl"]);
  b.api.on("POST /api/ui/t/button/exp/run", { success: true });
  b.window.cimUiRun("ui_btn_t_exp", btn);
  await b.tick(20);
  assert.deepEqual(b.api.last("/api/ui/t/button/exp/run", "POST").body, { filenames: ["p.jxl", "q.jxl"], option: "b" });
});

test("a declared section renders its status and runs its action", async () => {
  const b = boot();
  await b.tick(30);
  const pane = b.document.createElement("div");
  pane.id = "settings_pane_module_t";
  b.document.body.appendChild(pane);
  b.api.on("/api/ui/t/section/s/status", { success: true, rows: [{ label: "K", value: "V" }],
    list: [{ text: "warned", tone: "warn" }], table: { head: ["a"], rows: [["c1"]] }, log: [{ at: 1, text: "logged" }] });
  b.api.on("POST /api/ui/t/section/s/action_do", { success: true, message: "did it" });
  b.document.dispatchEvent(new b.window.CustomEvent("module-settings-tab", { detail: "t" }));
  await b.tick(20);
  for (const s of ["hello", "K", "V", "warned", "c1", "logged"]) assert.ok(pane.textContent.includes(s), s);
  pane.querySelector('[data-ui-action="do"]').click();
  await b.tick(20);
  assert.ok(b.api.last("/api/ui/t/section/s/action_do", "POST"));
  assert.match(pane.querySelector("[data-ui-msg]").textContent, /did it/);
});

test("a declared tile badge marks tiles whose row has the field", async () => {
  const b = boot();
  await b.tick(30);
  const tile = b.document.createElement("div");
  tile.className = "gallery-item";
  b.window.runGalleryTileHooks(tile, { filename: "x", fav: 3 });
  const badge = tile.querySelector(".cim-tile-badge");
  assert.ok(badge && badge.textContent === "FAV 3" && badge.classList.contains("cim-tile-badge-ok"));
  const plain = b.document.createElement("div");
  b.window.runGalleryTileHooks(plain, { filename: "y", fav: 0 });
  assert.equal(plain.querySelector(".cim-tile-badge"), null);
});

test("a section renders pre / link, fills an action's select from the view and keeps polling while live", async () => {
  const b = page({});
  b.api.on("/api/module_ui", { buttons: [], left_tabs: [], controls_tabs: [], tile_badges: [], import_tabs: [],
    sections: [{ pane: "k", id: "s", title: null, text: null, poll: 1, order: 1, feature: null, mount: null,
      status_url: "/api/k/status",
      actions: [{ id: "dl", label: "Download", url: "/api/k/download", variant: null, title: null, confirm: null, options_from: "builds" }],
      module_id: "k" }] });
  await b.tick(30);
  const pane = b.document.createElement("div");
  pane.id = "settings_pane_module_k";
  b.document.body.appendChild(pane);
  b.api.on("/api/k/status", { success: true, view: { rows: [{ label: "S", value: "ok" }], pre: ["l1", "l2"],
    link: { url: "http://x/", label: "rel" }, live: true, builds: [{ value: "a", label: "A" }, { value: "b", label: "B" }] } });
  b.api.on("POST /api/k/download", { success: true, message: "started" });
  b.document.dispatchEvent(new b.window.CustomEvent("module-settings-tab", { detail: "k" }));
  await b.tick(20);
  assert.match(pane.querySelector("[data-ui-pre]").textContent, /l1\nl2/);
  assert.equal(pane.querySelector("a").getAttribute("href"), "http://x/");
  const sel = pane.querySelector('select[data-ui-for="dl"]');
  assert.equal(sel.options.length, 2);
  sel.value = "b";
  pane.querySelector('[data-ui-action="dl"]').click();
  await b.tick(20);
  assert.deepEqual(b.api.last("/api/k/download", "POST").body, { option: "b" });
});