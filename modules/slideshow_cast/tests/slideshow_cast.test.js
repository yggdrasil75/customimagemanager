// Frontend tests for modules/slideshow_cast: the Cast button, the session and state mirroring.
const { page, test, assert, skipUnless } = require("cim");
const skip = skipUnless("slideshow_cast");

test("Cast creates a session and mirrors slideshow state into it", { skip }, async () => {
  const b = page({ modules: ["slideshow", "slideshow_cast"] });
  assert.deepEqual(b.errors, []);
  await b.tick(20);                            // DOMContentLoaded: buttons register then
  const SS = b.window.CIMSlideshow, SC = b.window.CIMSlideshowCast;
  assert.ok(SC, "window.CIMSlideshowCast is exposed");
  assert.ok((b.window._extButtons.slideshow_tools || []).some(h => /ssc_btn/.test(h)), "Cast button registered");
  b.api.on("POST /api/slideshow_cast/session", { success: true, token: "T1", url: "http://t/api/slideshow_cast/pub/T1/" });
  b.api.on("POST /api/slideshow_cast/session/T1/state", { success: true, version: 1, receiver_seen: true });
  SS.start({ files: ["a/1.jpg", "a/2.jpg"], prefs: { interval: 2, transition: "none" }, fullscreen: false });
  SC.show();
  assert.ok(b.document.getElementById("ssc_panel"), "panel built inside the overlay");
  await SC.viaLink();
  await b.tick(20);
  assert.equal(SC.session.token, "T1");
  const first = b.api.find("/api/slideshow_cast/session/T1/state", "POST")[0];
  assert.ok(first, "state pushed");
  assert.equal(first.body.files.length, 2, "playlist sent with the first push");
  assert.equal(b.document.getElementById("ssc_url").value, "http://t/api/slideshow_cast/pub/T1/");
  SS.next();
  await b.tick(20);
  const last = b.api.last("/api/slideshow_cast/session/T1/state", "POST");
  assert.equal(last.body.index, 1);
  assert.equal(last.body.files, undefined, "unchanged playlist is not resent");
  b.api.on("POST /api/slideshow_cast/session/T1/close", { success: true });
  await SC.disconnect();
  assert.equal(SC.session.token, null);
  SS.stop();
});
