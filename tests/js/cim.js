// Shared helper for every frontend test, core (tests/js) and module
// (modules/<id>/tests/*.test.js): `const { page, test, assert } = require("cim");`
//   page(opts)      boot the real page in jsdom; closed automatically after the file
//                   opts.modules: true | ["people"] also loads module JS
//                   opts.url, opts.prompt: location and window.prompt() answer
//   hasModule(id)   is that module enabled (so its JS is on the page)?
//   skipUnless(id)  node:test skip option for a module's own test file
const { test, after } = require("node:test");
const { boot, hasModule, moduleAssets } = require("./harness");
// Pages stay open until the whole file is done: a page's boot fetches settle
// asynchronously, and closing its window mid-flight turns them into errors.
const pages = [];
after(async () => {
  await new Promise(r => setTimeout(r, 50));
  for (const b of pages) b.window.close();
});
function page(opts) {
  const b = boot(opts);
  pages.push(b);
  return b;
}
const skipUnless = id => (hasModule(id) ? false : `module '${id}' is not enabled`);
module.exports = { page, test, after, hasModule, moduleAssets, skipUnless,
                   assert: require("node:assert/strict") };
