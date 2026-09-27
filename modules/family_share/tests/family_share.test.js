// Family share settings tab: edits are buffered and written by the modal's
// Save button; a reopened modal shows what the server has.
const { page, test, assert, skipUnless } = require("cim");

const STATE = () => ({ ok: true, instance: { id: "i", name: "me", pub_key: "p", fingerprint: "aaaa" },
  peers: [], rules: [], outbox: {}, received: 0, folders: [], albums: [],
  options: { family_share_name: "me", family_share_my_url: "", family_share_outbound: true, family_share_inbound: true,
    family_share_incoming_folder: "family", family_share_tag_received: "from:{peer}", revoke_on_unshare: true,
    revoke_on_delete: true, honor_revoke: true, reshare_received: false, match_unconfirmed_tags: false,
    share_all_albums: false, share_regions: true, family_share_interval_min: 10 },
  last_plan: null, last_plan_at: 0, dirty: false });

test("family share options save through the modal's Save button", { skip: skipUnless("family_share") }, async () => {
  const b = page({ modules: ["family_share"] });
  assert.deepEqual(b.errors, []);
  const server = STATE();
  b.api.on("/api/family_share/state", () => server);
  b.api.on("POST /api/update_settings", (c) => { Object.assign(server.options, c.body); return { success: true }; });
  // the real path: the gear opens the modal, the user clicks the module's tab button
  await b.run("openSettings()"); await b.tick(30);
  const tabBtn = b.document.querySelector('[data-settings-tab="module_family_share"]');
  assert.ok(tabBtn, "tab button rendered from the real /api/modules snapshot");
  tabBtn.click(); await b.tick(30);
  const pane = b.document.getElementById("settings_pane_module_family_share");
  assert.ok(pane && pane.querySelector("#fs_name"), "tab rendered");
  // edit: nothing goes to the server yet
  b.run(`const n = document.getElementById("fs_name"); n.value = "kiddo"; n.dispatchEvent(new Event("change"));
         const t = document.querySelector('[data-opt="revoke_on_delete"]'); t.checked = false; t.dispatchEvent(new Event("change"));`);
  assert.equal(b.api.find("/api/update_settings", "POST").length, 0);
  // jsdom does not run inline onclick= handlers, so call what the Save button calls
  await b.run("saveAllSettings()"); await b.tick(60);
  assert.ok(b.document.getElementById("settings_modal").classList.contains("hidden"), "modal closed after Save");
  const posts = b.api.find("/api/update_settings", "POST").map(c => c.body);
  const mine = posts.find(p => p && "family_share_name" in p);
  assert.deepEqual(mine, { family_share_name: "kiddo", revoke_on_delete: false });
  // reopen: shows the saved values
  await b.run("openSettings()"); await b.tick(30);
  b.document.querySelector('[data-settings-tab="module_family_share"]').click(); await b.tick(30);
  assert.equal(b.document.getElementById("fs_name").value, "kiddo");
  assert.equal(b.document.querySelector('[data-opt="revoke_on_delete"]').checked, false);
});
