// Frontend test for modules/email: its settings block is declared in module.py
// (host.add_settings_section) and drawn by the core's module_ui.js; the status
// renders and the test button posts to the declared action.
const { page, test, assert, skipUnless } = require("cim");
const skip = skipUnless("email");

test("Email pane shows status and sends a test mail", { skip }, async () => {
  const b = page({ modules: ["email"] });
  assert.deepEqual(b.errors, []);
  const pane = b.document.createElement("div");
  pane.id = "settings_pane_module_email";
  b.document.body.appendChild(pane);
  await b.tick(10);
  b.api.on("/api/email/status", { success: true, configured: true, recipients: ["a@x.org"],
    view: { rows: [{ label: "Admin recipients", value: "a@x.org" }], log: [{ at: 1700000000, text: "error: test - boom" }] } });
  b.api.on("POST /api/email/test", { success: true, recipients: ["a@x.org"], message: "Test mail sent to a@x.org" });
  b.document.dispatchEvent(new b.window.CustomEvent("module-settings-tab", { detail: "email" }));
  await b.tick(20);
  const btn = pane.querySelector('[data-ui-action="test"]');
  assert.ok(btn, "test button rendered");
  assert.match(pane.textContent, /a@x.org/);
  assert.match(pane.textContent, /boom/);
  btn.click();
  await b.tick(20);
  assert.ok(b.api.last("/api/email/test", "POST"), "clicked -> POST /api/email/test");
  assert.match(pane.querySelector("[data-ui-msg]").textContent, /Test mail sent to a@x.org/);
});