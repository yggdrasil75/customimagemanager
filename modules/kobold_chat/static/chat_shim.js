/**
 * @brief Library chat shim, loaded in the <head> of the proxied koboldcpp pages.
 *
 * 1. Adds the session's CSRF token to the page's own non-GET requests (fetch and
 *    XMLHttpRequest), which the app's login gate requires; koboldcpp's pages know
 *    nothing about it.
 * 2. Draws the approval panel: the user's pending edit proposals from the chat
 *    tools, each with Approve / Deny. Nothing a proposal describes happens before
 *    Approve; the server checks every permission again at that moment.
 */
(function () {
  "use strict";
  var CFG = window.CIM_CHAT || {};
  var SAFE = { GET: 1, HEAD: 1, OPTIONS: 1 };

  /** @brief True for a URL on this origin. */
  function sameOrigin(u) {
    try { return new URL(u, location.href).origin === location.origin; } catch (e) { return false; }
  }

  // -- CSRF on the page's requests -------------------------------------------------
  if (CFG.csrf && window.fetch) {
    var origFetch = window.fetch;
    window.fetch = function (input, init) {
      var isReq = (typeof Request !== "undefined") && input instanceof Request;
      var url = isReq ? input.url : String(input);
      var method = String((init && init.method) || (isReq ? input.method : "GET")).toUpperCase();
      if (!SAFE[method] && sameOrigin(url)) {
        var h = new Headers((init && init.headers) || (isReq ? input.headers : undefined));
        if (!h.has("X-CSRF-Token")) h.set("X-CSRF-Token", CFG.csrf);
        init = Object.assign({}, init || {}, { headers: h });
      }
      return origFetch.call(this, input, init);
    };
  }
  if (CFG.csrf && window.XMLHttpRequest) {
    var origOpen = XMLHttpRequest.prototype.open;
    var origSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (method, url) {
      this._cimNeedsCsrf = !SAFE[String(method || "GET").toUpperCase()] && sameOrigin(url);
      return origOpen.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function () {
      if (this._cimNeedsCsrf) {
        try { this.setRequestHeader("X-CSRF-Token", CFG.csrf); } catch (e) { /* already sent */ }
      }
      return origSend.apply(this, arguments);
    };
  }

  // -- approval panel ----------------------------------------------------------------
  var panel = null, list = null, badge = null, open = false, last = "";

  /** @brief One element with inline styles (the page carries none of the app's CSS). */
  function el(tag, css, text) {
    var e = document.createElement(tag);
    if (css) e.style.cssText = css;
    if (text != null) e.textContent = text;
    return e;
  }

  /** @brief A small button in the panel. */
  function btn(label, bg, onclick) {
    var b = el("button", "border:0;border-radius:6px;padding:3px 10px;margin-left:6px;cursor:pointer;" +
      "color:#fff;font:12px system-ui,sans-serif;background:" + bg, label);
    b.type = "button";
    b.onclick = onclick;
    return b;
  }

  /** @brief Build the floating badge + panel once. */
  function build() {
    if (panel || !document.body) return;
    badge = el("button", "position:fixed;left:12px;bottom:12px;z-index:2147483646;display:none;" +
      "border:0;border-radius:999px;padding:6px 12px;cursor:pointer;font:600 12px system-ui,sans-serif;" +
      "color:#fff;background:var(--cim-warn-600, #d97706);box-shadow:0 2px 8px rgba(0,0,0,.35)");
    badge.type = "button";
    badge.title = "Edits proposed by the chat, waiting for your approval";
    badge.onclick = function () { open = !open; panel.style.display = open ? "block" : "none"; };
    panel = el("div", "position:fixed;left:12px;bottom:52px;z-index:2147483647;display:none;" +
      "width:min(420px,calc(100vw - 24px));max-height:60vh;overflow:auto;border-radius:10px;" +
      "padding:10px;font:13px system-ui,sans-serif;color:#e5e7eb;background:#1f2937;" +
      "box-shadow:0 4px 18px rgba(0,0,0,.45)");
    panel.appendChild(el("div", "font-weight:600;margin-bottom:6px", "Proposed changes"));
    panel.appendChild(el("div", "color:#9ca3af;font-size:11px;margin-bottom:8px",
      "The chat only proposes edits. Nothing changes until you approve."));
    list = el("div");
    panel.appendChild(list);
    document.body.appendChild(badge);
    document.body.appendChild(panel);
  }

  /** @brief POST approve / deny for one proposal, then refresh. */
  function decide(id, verb) {
    var h = { "Content-Type": "application/json" };
    if (CFG.csrf) h["X-CSRF-Token"] = CFG.csrf;
    fetch(CFG.actions + "/" + id + "/" + verb, { method: "POST", headers: h, body: "{}" })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j.success) alert("Not applied: " + (j.error || "failed"));
        last = "";
        poll();
      })
      .catch(function () { alert("Could not reach the server."); });
  }

  /** @brief Redraw the list of pending proposals. */
  function render(actions) {
    build();
    if (!panel) return;
    var sig = actions.map(function (a) { return a.id; }).join(",");
    if (sig === last) return;
    last = sig;
    list.textContent = "";
    actions.forEach(function (a) {
      var row = el("div", "border-top:1px solid #374151;padding:6px 0;display:flex;align-items:flex-start");
      var txt = el("div", "flex:1;word-break:break-word", "#" + a.id + " " + a.summary);
      row.appendChild(txt);
      if (CFG.can_edit) row.appendChild(btn("Approve", "var(--cim-ok-600, #16a34a)", function () { decide(a.id, "approve"); }));
      row.appendChild(btn("Deny", "var(--cim-danger-600, #dc2626)", function () { decide(a.id, "deny"); }));
      list.appendChild(row);
    });
    badge.textContent = actions.length + " change" + (actions.length === 1 ? "" : "s") + " to approve";
    badge.style.display = actions.length ? "block" : "none";
    if (!actions.length) { open = false; panel.style.display = "none"; }
  }

  /** @brief Fetch the pending proposals. */
  function poll() {
    if (!CFG.actions || document.hidden) return;
    fetch(CFG.actions + "?status=pending", { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) { if (j && j.success) render(j.actions || []); })
      .catch(function () { /* the server is restarting; try again later */ });
  }

  if (CFG.can_edit) {
    document.addEventListener("DOMContentLoaded", poll);
    document.addEventListener("visibilitychange", poll);
    setInterval(poll, 4000);
  }
})();