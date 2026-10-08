// Frontend test for modules/email: the tab's tools render and the test button posts.
const { page, test, assert, skipUnless } = require("cim");
const skip = skipUnless("email");

test("Email pane shows status and sends a test mail", { skip }, async () => {
  const b = page({ modules: ["email"] });
  assert.deepEqual(b.errors, []);
  const pane = b.document.createElement("div");
  pane.id = "settings_pane_module_email";
  b.document.body.appendChild(pane);
  b.api.on("/api/email/status", { success: true, configured: true, recipients: ["a@x.org"],
    log: [{ at: 1700000000, kind: "error", subject: "test", detail: "boom" }] });
  b.api.on("POST /api/email/test", { success: true, recipients: ["a@x.org"] });
  b.document.dispatchEvent(new b.window.CustomEvent("module-settings-tab", { detail: "email" }));
  await b.tick(20);
  assert.ok(pane.querySelector("#em_test"), "test button rendered");
  assert.match(pane.querySelector("#em_status").textContent, /a@x.org/);
  assert.match(pane.querySelector("#em_status").textContent, /boom/);
  pane.querySelector("#em_test").click();
  await b.tick(20);
  assert.ok(b.api.last("/api/email/test", "POST"), "clicked -> POST /api/email/test");
  assert.match(pane.querySelector("#em_msg").textContent, /Test mail sent to a@x.org/);
});