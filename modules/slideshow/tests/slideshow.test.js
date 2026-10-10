// Frontend tests for modules/slideshow: the overlay, stepping, pause and stop.
const { page, test, assert, skipUnless } = require("cim");
const skip = skipUnless("slideshow");

function boot() {
  const b = page({ modules: ["slideshow"] });
  assert.deepEqual(b.errors, []);
  return b;
}

test("start shows the first slide, steps and stops", { skip }, async () => {
  const b = boot();
  const SS = b.window.CIMSlideshow;
  assert.ok(SS, "window.CIMSlideshow is exposed");
  const events = [];
  b.window.addEventListener("cim:slideshow", e => events.push(e.detail.state));
  assert.ok(SS.start({ files: ["a/1.jpg", "a/2.jpg", { filename: "a/3.mp4", kind: "video" }],
                       prefs: { interval: 1, transition: "none", videos: "interval" }, fullscreen: false }));
  const ov = b.document.getElementById("ss_overlay");
  assert.ok(ov && !ov.classList.contains("hidden"), "overlay visible");
  assert.equal(SS.state.index, 0);
  assert.equal(SS.state.file, "a/1.jpg");
  assert.match(b.document.getElementById("ss_counter").textContent, /1 \/ 3/);
  SS.next();
  assert.equal(SS.state.file, "a/2.jpg");
  SS.next();
  assert.equal(SS.state.kind, "video");
  assert.ok(b.document.getElementById("ss_video").classList.contains("ss-on"), "video layer shown");
  SS.prev();
  assert.equal(SS.state.file, "a/2.jpg");
  SS.toggle();
  assert.equal(SS.state.paused, true);
  SS.toggle();
  assert.equal(SS.state.paused, false);
  SS.stop();
  assert.equal(SS.state.running, false);
  assert.ok(ov.classList.contains("hidden"), "overlay hidden after stop");
  assert.deepEqual(events.slice(0, 2), ["start", "slide"]);
  assert.equal(events[events.length - 1], "stop");
});

test("videos=skip drops videos; loop=false ends the show", { skip }, async () => {
  const b = boot();
  const SS = b.window.CIMSlideshow;
  SS.start({ files: [{ filename: "v.mp4", kind: "video" }, "p.jpg"],
             prefs: { interval: 1, loop: false, videos: "skip" }, fullscreen: false });
  assert.equal(SS.state.total, 1);
  SS.next();                                   // past the end, no loop: stops
  assert.equal(SS.state.running, false);
});

test("startQuery asks the server for the gallery playlist", { skip }, async () => {
  const b = boot();
  const SS = b.window.CIMSlideshow;
  b.api.on("/api/slideshow/prefs", { success: true, prefs: { interval: 3, shuffle: false, loop: true, transition: "fade", videos: "play" } });
  b.api.on("/api/slideshow/list", { success: true, files: [{ filename: "q/1.jpg", kind: "image" }], total: 1 });
  b.window.currentFile = "q/1.jpg";
  await SS.startQuery();
  const call = b.api.last("/api/slideshow/list");
  assert.ok(call, "playlist requested");
  assert.equal(call.query.start, "q/1.jpg");
  assert.equal(SS.state.prefs.interval, 3);
  SS.stop();
});

test("buttons are registered in the popout and the gallery", { skip }, async () => {
  const b = boot();
  await b.tick(20);                            // DOMContentLoaded: buttons register then
  const btns = b.window._extButtons || {};
  assert.ok((btns.popout_tools || []).some(h => /ss_btn_popout/.test(h)));
  assert.ok(!(btns.viewer_toggles || []).some(h => /ss_btn/.test(h)), "no slideshow button under the viewer");
  assert.ok((btns.gallery_tools || []).some(h => /ss_btn_gallery/.test(h)));
});

test("prefs changed in the overlay are saved for the user", { skip }, async () => {
  const b = boot();
  const SS = b.window.CIMSlideshow;
  b.api.on("POST /api/user/settings", { success: true, fields: [] });
  SS.start({ files: ["a.jpg", "b.jpg"], prefs: { interval: 5, loop: true, transition: "fade" }, fullscreen: false });
  SS.toggleLoop();
  SS.setTransition("none");
  SS.setInterval(7);
  await b.tick(700);
  const call = b.api.last("/api/user/settings", "POST");
  assert.ok(call, "prefs posted");
  const body = JSON.parse(JSON.stringify(call.body));
  assert.deepEqual(body, { slideshow_loop: false, slideshow_transition: "none", slideshow_interval: 7 });
  assert.ok(b.document.getElementById("ss_overlay").classList.contains("ss-cut"));
  SS.stop();
});