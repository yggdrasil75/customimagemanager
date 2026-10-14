// Frontend test for modules/storage_alerts: its Alerts block is declared in
// module.py (host.add_settings_section, mounted in Settings -> Storage) and
// drawn by the core's module_ui.js when the Storage tab opens.
const { page, test, assert, skipUnless } = require("cim");
const skip = skipUnless("storage_alerts");

test("Storage alerts block shows readings and sends a test mail", { skip }, async () => {
  const b = page({ modules: ["storage_alerts"] });
  assert.deepEqual(b.errors, []);
  await b.tick(10);
  const mount = b.document.getElementById("storage_alerts_section");
  assert.ok(mount, "the Storage pane has the alerts mount");
  b.api.on("/api/storage_alerts/status", { success: true, view: {
    rows: [{ label: "Recipients", value: "a@x.org" }],
    list: [{ text: "Low disk space: media folder: 1.0 GB free", tone: "warn" }],
    table: { rows: [{ cells: ["media folder", "1.0 GB free", "of 100.0 GB, floor 2.0 GB", "/m"], tone: "warn" }] } } });
  b.api.on("POST /api/storage_alerts/test", { success: true, recipients: ["a@x.org"], message: "Test mail sent to a@x.org" });
  b.document.dispatchEvent(new b.window.CustomEvent("cim:settings-tab", { detail: "storage" }));
  await b.tick(20);
  assert.ok(!mount.classList.contains("hidden"), "mount unhidden");
  const btn = mount.querySelector('[data-ui-action="test"]');
  assert.ok(btn, "test button rendered");
  assert.match(mount.textContent, /Low disk space: media folder/);
  btn.click();
  await b.tick(20);
  assert.ok(b.api.last("/api/storage_alerts/test", "POST"), "clicked -> POST /api/storage_alerts/test");
  assert.match(mount.querySelector("[data-ui-msg]").textContent, /Test mail sent to a@x.org/);
});