// Shared thumbnail loader (CIMLazy, globals.js): thumbnails load near the viewport and are
// dropped again once far from it, for gallery tiles, background chips and lazy <img>s.
const { page, test, assert } = require("cim");

// A fake IntersectionObserver: tests say which elements are near/far by hand.
const observers = [];
class FakeIO {
  constructor(cb, opts) { this.cb = cb; this.margin = opts && opts.rootMargin; this.els = new Set(); observers.push(this); }
  observe(el) { this.els.add(el); }
  unobserve(el) { this.els.delete(el); }
  disconnect() { this.els.clear(); }
}
const b = page({ IntersectionObserver: FakeIO });
const near = () => observers.find(o => o.margin === "300px");
const far = () => observers.find(o => o.margin === "1500px");
const fire = (o, el, isIntersecting) =>
  o.cb([{ target: el, isIntersecting, boundingClientRect: { width: 80, height: 60 } }]);

test("gallery tiles load near the viewport and unload far from it", () => {
  b.run(`renderGallery([{filename:"a.jxl",kind:"image",width:10,height:10,tags:[],description:""}])`);
  const tile = b.document.querySelector("#gallery_grid .gallery-item");
  const img = tile.querySelector("img");
  assert.ok(near().els.has(tile) && far().els.has(tile));
  fire(near(), tile, true);
  assert.equal(img.getAttribute("src"), "/api/thumb/a.jxl");
  fire(far(), tile, false);
  assert.equal(img.getAttribute("src"), null);
  assert.ok(!img.classList.contains("loaded"));
  fire(near(), tile, true);                                // scrolled back: loads again
  assert.equal(img.getAttribute("src"), "/api/thumb/a.jxl");
});

test("background chips and lazy images are picked up when added", async () => {
  const box = b.document.createElement("div");
  box.innerHTML = `<div class="chip" data-thumb="/api/thumb/p.jxl"></div><img loading="lazy" src="/api/thumb/q.jxl">`;
  b.document.body.appendChild(box);
  await b.tick(0);
  const chip = box.querySelector(".chip"), img = box.querySelector("img");
  assert.ok(near().els.has(chip) && near().els.has(img));
  fire(near(), chip, true);
  assert.match(chip.style.backgroundImage, /p\.jxl/);
  fire(far(), chip, false);
  assert.equal(chip.style.backgroundImage, "");
  fire(far(), img, false);
  assert.equal(img.getAttribute("src"), null);
  assert.equal(img.style.minHeight, "60px");               // keeps its box while empty
  fire(near(), img, true);
  assert.equal(img.getAttribute("src"), "/api/thumb/q.jxl");
  assert.equal(img.style.minHeight, "");
});

test("tiles removed from the page stop being tracked", () => {
  b.run(`renderGallery([{filename:"z.jxl",kind:"image",width:10,height:10,tags:[],description:""}])`);
  const tile = b.document.querySelector("#gallery_grid .gallery-item");
  b.run(`document.getElementById('gallery_grid').innerHTML=''`);
  fire(far(), tile, false);
  assert.ok(!near().els.has(tile) && !far().els.has(tile));
  assert.ok(!b.run("CIMLazy.tracked").has(tile));
});