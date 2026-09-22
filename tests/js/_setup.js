// Per-file helper: boot the page once, close the window (kills timers) at end.
const { test, after } = require("node:test");
const { boot } = require("./harness");
function page(opts) {
  const b = boot(opts);
  after(() => b.window.close());
  return b;
}
module.exports = { page, test, after, assert: require("node:assert/strict") };
