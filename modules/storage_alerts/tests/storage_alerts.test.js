// Frontend test for modules/storage_alerts: the tab's tools render and call the routes.
const { page, test, assert, skipUnless } = require("cim");
const skip = skipUnless("storage_alerts");

test("Storage alerts pane shows status and sends a test mail", { skip }, async () => {
  const b = page({ modules: ["storage_alerts"] });
  assert.deepEqual(b.errors, []);
  const pane = b.document.createElement("div");
  pane.id = "settings_pane_module_storage_alerts";
  b.document.body.appendChild(pane);
  b.api.on("/api/storage_alerts/status", { success: true, enabled: true, at: 1700000000, next_check: 1700003600,
    recipients: ["a@x.org"], alerts: { "disk:media folder": { title: "Low disk space: media folder", detail: "1.0 GB free" } },
    readings: [{ what: "media folder", path: "/m", free: 1 << 30, total: 100 * (1 << 30), floor: 2 * (1 << 30) }], log: [] });
  b.api.on("POST /api/storage_alerts/test", { success: true, recipients: ["a@x.org"] });
  b.document.dispatchEvent(new b.window.CustomEvent("module-settings-tab", { detail: "storage_alerts" }));
  await b.tick(20);
  assert.ok(pane.querySelector("#sa_test"), "test button rendered");
  assert.match(pane.querySelector("#sa_status").textContent, /Low disk space: media folder/);
  pane.querySelector("#sa_test").click();
  await b.tick(20);
  assert.ok(b.api.last("/api/storage_alerts/test", "POST"), "clicked -> POST /api/storage_alerts/test");
  assert.match(pane.querySelector("#sa_msg").textContent, /Test mail sent to a@x.org/);
});
