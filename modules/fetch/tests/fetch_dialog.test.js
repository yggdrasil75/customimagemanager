// The Fetch dialog belongs to the fetch module: it opens with gallery-dl off and
// queues URLs for whichever fetcher takes them; gallery-dl's own parts (field
// check, login, mapping) show only while that fetcher is registered.
const { page, test, assert, skipUnless } = require("cim");
const skip = skipUnless("fetch");

function boot(fetchers) {
  const b = page({ modules: ["fetch"] });
  b.api.on("/api/fetch/queue", { success: true, queue: [], fetchers, paused: "" });
  b.api.on("POST /api/fetch/which", c => String(c.body.target).includes("youtube")
    ? { success: true, id: "ytdlp", label: "yt-dlp" } : { success: true, id: null, label: null });
  b.api.on("POST /api/fetch/add", c => ({ success: true, added: c.body.targets.length,
    fetchers: Object.fromEntries(c.body.targets.map(t => [t, t.includes("youtube") ? "yt-dlp" : null])) }));
  return b;
}
const shown = el => !el.classList.contains("hidden");

test("opens and queues with gallery-dl off", { skip }, async () => {
  const b = boot([{ id: "ytdlp", label: "yt-dlp", available: true }]);
  assert.deepEqual(b.errors, []);
  const $ = id => b.document.getElementById(id);
  await b.tick(10);
  assert.ok(shown($("btn_gdl")), "the header button shows once the fetch module is loaded");
  await b.run("gdlOpen()");          // the button's onclick (inline handlers don't run in jsdom)
  await b.tick(10);
  assert.ok(shown($("gdl_modal")), "dialog opens without the gallery-dl module");
  assert.equal($("fetch_fetchers").textContent, "Fetchers: yt-dlp");
  const gdlOnly = [...b.document.querySelectorAll('#gdl_modal [data-fetcher-only="gallerydl"]')];
  assert.ok(gdlOnly.length >= 3 && gdlOnly.every(el => !shown(el)), "gallery-dl parts hidden");
  assert.ok(!shown($("gdl_missing")));

  $("gdl_url").value = "https://www.youtube.com/watch?v=x";
  await b.run("gdlUrlChanged()");    // its onblur
  await b.tick(10);
  assert.equal($("fetch_url_who").textContent, "Fetched with yt-dlp.");
  assert.equal(b.api.find("/api/gdl/").length, 0, "no gallery-dl calls");

  await b.run("gdlFetch()");         // its onclick
  await b.tick(10);
  assert.deepEqual(b.api.last("/api/fetch/add", "POST").body.targets, ["https://www.youtube.com/watch?v=x"]);
  assert.match($("gdl_status").textContent, /Added 1 download/);
  assert.equal(b.api.find("/api/gdl/").length, 0, "still no gallery-dl calls");
});

test("a URL nothing handles is flagged", { skip }, async () => {
  const b = boot([{ id: "ytdlp", label: "yt-dlp", available: true }]);
  const $ = id => b.document.getElementById(id);
  await b.run("gdlOpen()");
  $("gdl_url").value = "https://example.org/a";
  await b.run("gdlUrlChanged()");    // its onblur
  await b.tick(10);
  assert.match($("fetch_url_who").textContent, /No fetcher/);
  await b.run("gdlFetch()");         // its onclick
  await b.tick(10);
  assert.match($("gdl_status").textContent, /No fetcher handles https:\/\/example\.org\/a/);
});

test("gallery-dl parts show with gallery-dl on; missing tools are named", { skip }, async () => {
  const b = boot([{ id: "gallerydl", label: "gallery-dl", available: true },
                  { id: "ytdlp", label: "yt-dlp", available: false }]);
  const $ = id => b.document.getElementById(id);
  await b.run("gdlOpen()");
  await b.tick(10);
  const gdlOnly = [...b.document.querySelectorAll('#gdl_modal [data-fetcher-only="gallerydl"]')];
  assert.ok(gdlOnly.filter(el => el.id !== "gdl_mapping").every(shown), "gallery-dl parts shown");
  assert.ok(!shown($("gdl_mapping")), "the mapping waits for Check fields");
  assert.ok(shown($("gdl_missing")));
  assert.match($("gdl_missing").textContent, /yt-dlp is on but not installed \(pip install yt-dlp/);
});