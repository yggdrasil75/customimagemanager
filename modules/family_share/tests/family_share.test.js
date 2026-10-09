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

// Settings -> My devices: a user's own phones, paired as their account.
test("my devices tab lists the account's phones and adds one", { skip: skipUnless("family_share") }, async () => {
  const b = page({ modules: ["family_share"] });
  assert.deepEqual(b.errors, []);
  const server = { ok: true, scopes: ["personal", "all"], inbound: true,
    me: { id: 7, username: "ann", display_name: "Ann" },
    server: { name: "home", fingerprint: "ffff", my_url: "https://home.example" },
    devices: [{ id: 3, name: "ann-pixel", folder: "", scope: "personal", enabled: 1, last_ok: 0, paired: true,
                fingerprint: "abcd", owner: "ann", owner_display: "Ann" }] };
  b.api.on("/api/family_share/devices", () => server);
  b.api.on("POST /api/family_share/devices/save", (c) => {
    server.devices.push({ id: 4, name: c.body.name, folder: "", scope: c.body.scope, enabled: 1, paired: false });
    return { ok: true, id: 4, peer: { name: c.body.name } };
  });
  b.api.on("POST /api/family_share/devices/key", () => ({ ok: true, name: "home", pairing_code: "fs1.x", fingerprint: "ffff", peer_name: "ann-tab" }));
  await b.run("openSettings()"); await b.tick(30);
  const tabBtn = b.document.querySelector('[data-settings-tab="module_family_devices"]');
  assert.ok(tabBtn, "My devices tab button rendered");
  tabBtn.click(); await b.tick(30);
  const pane = b.document.getElementById("settings_pane_module_family_devices");
  assert.ok(pane.textContent.includes("paired as Ann"), "shows the account");
  assert.equal(pane.querySelector('tr[data-id="3"] .fd-name').value, "ann-pixel");
  b.run(`const tr = document.querySelector("#settings_pane_module_family_devices tr.fs-new");
         tr.querySelector(".fd-name").value = "ann-tab"; tr.querySelector(".fd-scope").value = "all";
         tr.querySelector(".fd-add").click();`);
  await b.tick(60);
  const posts = b.api.find("/api/family_share/devices/save", "POST").map(c => c.body);
  assert.equal(posts.length, 1);
  assert.equal(posts[0].name, "ann-tab"); assert.equal(posts[0].scope, "all");
  assert.ok(!("user_id" in posts[0]), "the browser never picks the owner");
  assert.ok(pane.querySelector(".fs-keybox input").value === "fs1.x", "pairing code for the app shown");
});
