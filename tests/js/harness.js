// Loads the REAL rendered app page (tests/js/_app.html, written by
// tests/test_frontend.py from Flask's `/`) into jsdom, evaluates the real
// static/*.js in the page's <script> order, and stubs the two things jsdom
// lacks: <canvas> 2D contexts and fetch(). fetch is routed to an in-memory
// API stub so every call is recorded and any response can be scripted.
const fs = require("fs"), path = require("path"), vm = require("vm");
const { JSDOM } = require("jsdom");

const ROOT = path.resolve(__dirname, "..", "..");
const HTML = path.join(__dirname, "_app.html");

function makeApi() {
  const calls = [];
  const routes = {};            // "METHOD /api/x" or "/api/x" → object | fn(url, init)
  const defaults = {
    "/api/state": { classes: ["object"], available_models: [], brand_name: "T", search_quick_filters: [] },
    "/api/folders": { success: true, folders: [{ path: "/", count: 0 }] },
    "/api/albums": { success: true, albums: [] },
    "/api/list": { success: true, files: [], total: 0, page: 0, page_size: 200 },
    "/api/review_list": { success: true, total: 0, items: [], counts: {} },
    "/api/box_labels": { success: true, labels: [] },
    "/api/auth/me": { user: { username: "t", is_admin: true, features: {} }, csrf: "x" },
    "/api/auth/config": { enabled: false, mode: "local" },
    "/api/modules": { success: true, modules: [], settings_tabs: [] },
    "/api/module_assets": { success: true, assets: [], js: [], css: [] },
    "/api/features": { success: true },
    "/api/metadata": { success: true, metadata: { tags: [], description: "", regions: [], albums: [] } },
  };
  const api = {
    calls,
    on(key, val) { routes[key] = val; return api; },
    reset() { calls.length = 0; for (const k in routes) delete routes[k]; },
    find(pathPrefix, method) {
      return calls.filter(c => c.path.startsWith(pathPrefix) && (!method || c.method === method));
    },
    last(pathPrefix, method) { const f = api.find(pathPrefix, method); return f[f.length - 1]; },
    fetch(url, init = {}) {
      const u = new URL(String(url), "http://t/");
      const method = (init.method || "GET").toUpperCase();
      let body = init.body;
      if (typeof body === "string") { try { body = JSON.parse(body); } catch { } }
      const call = { url: String(url), path: u.pathname, query: Object.fromEntries(u.searchParams), method, body };
      calls.push(call);
      let r = routes[`${method} ${u.pathname}`] ?? routes[u.pathname] ?? defaults[u.pathname];
      if (typeof r === "function") r = r(call);
      if (r === undefined) r = { success: true };
      const status = r && r.__status || 200;
      const text = JSON.stringify(r);
      return Promise.resolve({
        ok: status < 400, status, headers: { get: () => "application/json" },
        json: () => Promise.resolve(JSON.parse(text)),
        text: () => Promise.resolve(text),
        blob: () => Promise.resolve(new Blob([text])),
      });
    },
  };
  return api;
}

function stubCanvas(window) {
  const noop = () => { };
  const ctx = new Proxy({}, {
    get: (t, k) => {
      if (k === "measureText") return () => ({ width: 10 });
      if (k === "getImageData") return () => ({ data: new Uint8ClampedArray(4) });
      if (k === "canvas") return {};
      return typeof t[k] === "undefined" ? noop : t[k];
    }, set: () => true,
  });
  window.HTMLCanvasElement.prototype.getContext = () => ctx;
  window.HTMLCanvasElement.prototype.toDataURL = () => "data:,";
}

/** boot({url}) → {window, document, api, run(js)}. */
function boot(opts = {}) {
  if (!fs.existsSync(HTML)) throw new Error("tests/js/_app.html missing — run via pytest (tests/test_frontend.py) or ./run_tests.sh");
  const html = fs.readFileSync(HTML, "utf8");
  const scripts = [...html.matchAll(/<script src="([^"]+)"><\/script>/g)].map(m => m[1])
    .filter(s => s.startsWith("/static/") && !s.includes("/vendor/"));
  const api = makeApi();
  const dom = new JSDOM(html.replace(/<script[^>]*src=[^>]*><\/script>/g, ""), {
    url: opts.url || "http://t/",
    runScripts: "outside-only", pretendToBeVisual: true,
  });
  const { window } = dom;
  window.fetch = api.fetch;
  window.THREE = {};                       // vendor three.js not loaded
  window.alert = () => { }; window.confirm = () => true; window.prompt = () => opts.prompt ?? null;
  window.scrollTo = () => { }; window.requestAnimationFrame = fn => setTimeout(fn, 0);
  window.ResizeObserver = class { observe() { } disconnect() { } unobserve() { } };
  window.IntersectionObserver = class { observe() { } disconnect() { } unobserve() { } };
  window.URL.createObjectURL = () => "blob:x"; window.URL.revokeObjectURL = () => { };
  window.HTMLMediaElement.prototype.play = () => Promise.resolve();
  window.HTMLMediaElement.prototype.pause = () => { };
  stubCanvas(window);
  const ctx = dom.getInternalVMContext();
  const run = (code, name = "inline") => vm.runInContext(code, ctx, { filename: name });
  const errors = [];
  for (const s of scripts) {
    const p = path.join(ROOT, s);
    try { run(fs.readFileSync(p, "utf8"), s); }
    catch (e) { errors.push(`${s}: ${e && e.stack || e}`); }
  }
  const tick = (ms = 0) => new Promise(r => setTimeout(r, ms));
  // val(): like run() but returns a plain-realm copy, so node's strict
  // deepEqual doesn't trip over jsdom-realm Array/Object prototypes.
  const val = (code, name) => JSON.parse(JSON.stringify(run(code, name)));
  return { window, document: window.document, api, run, val, errors, tick, dom };
}

module.exports = { boot, makeApi };
