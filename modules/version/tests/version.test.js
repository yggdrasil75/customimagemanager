// Version module front end: opening Settings -> Info still renders the core
// sections (search filters, the About {label, value} rows) and adds the
// Changelog panel with Unreleased expanded and the update banner.
const { page, test, assert, skipUnless } = require("cim");
const skip = skipUnless("version");

test("Info tab renders sections, the changelog panel and the update banner", { skip }, async () => {
  const b = page({ modules: ["version"] });
  assert.deepEqual(b.errors, []);
  b.api.on("/api/info", { success: true, sections: [
    { id: "search", title: "Search filters", rows: [{ token: "tag:", help: "by tag", source: "core" }] },
    { id: "about", title: "About", rows: [{ label: "Version", value: "1.0.0-dev" },
      { label: "Latest release", value: "v1.1.0 - update available", url: "https://x/rel" }] },
  ] });
  b.api.on("/api/version", { success: true, version: "1.0.0-dev", commit: "", date: "", channel: "release",
    latest: { version: "v1.1.0", url: "https://x/rel", date: "", sha: "" }, update_available: true,
    checked_at: "2026-01-01T00:00:00", error: "", repo: "o/r" });
  b.api.on("/api/version/changelog", { success: true, raw: "", releases: [
    { version: "Unreleased", date: "", sections: { Added: ["A thing"] } },
    { version: "1.0.0", date: "2026-01-01", sections: { Fixed: ["A bug"] } },
  ] });
  await b.window.loadInfoTab();
  await b.tick(20);
  const info = b.document.getElementById("info_sections");
  assert.ok(info.querySelector('[data-info-section="search"] code'), "search filter rendered");
  const about = info.querySelector('[data-info-section="about"]');
  assert.ok(about, "about section rendered");
  assert.equal(info.firstElementChild, about, "about first");
  assert.match(about.textContent, /1\.0\.0-dev/);
  assert.ok(about.querySelector('a[href="https://x/rel"]'), "latest release links out");
  const panel = b.document.getElementById("version_panel");
  assert.ok(panel, "changelog panel mounted");
  const rels = panel.querySelectorAll("details.version-release");
  assert.equal(rels.length, 2);
  assert.ok(rels[0].open, "Unreleased expanded");
  assert.ok(!rels[1].open, "older release collapsed");
  assert.match(panel.querySelector("#version_banner").textContent, /Update available: v1\.1\.0/);
  assert.ok(panel.querySelector("#version_check_btn"), "check button rendered");
  // a second open re-renders in place
  await b.window.loadInfoTab();
  await b.tick(20);
  assert.equal(b.document.querySelectorAll("#version_panel").length, 1);
});
