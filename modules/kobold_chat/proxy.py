"""! @file
@brief Reverse proxy from /chat/ and /chatk/ to the local koboldcpp server.
======================================================================
Both koboldcpp pages build every URL relative to where they are served, so
mounting them under a prefix of this app is enough: the llama.cpp web UI
(koboldcpp's /lcpp/) at /chat/, KoboldAI Lite (koboldcpp's /) at /chatk/.

What the proxy changes on the way:

- the HTML gets two scripts in its <head>: the session values and the shim
  that adds the CSRF header to the page's own requests and draws the
  approval panel;
- /v1/chat/completions goes through the tool loop (agent.py);
- Lite's /api/extra/version reports the server-wide extras it must not use
  (shared save slots, multiplayer, web search, admin) as absent;
- endpoints that would act outside the user's permissions are refused:
  koboldcpp's own MCP bridge and CORS proxy (server-side fetches of any URL),
  shared save slots, multiplayer, web search, shutdown, admin and model
  (un)loading - the last two stay open to admins.

The browser's cookies and Authorization header are never forwarded.
"""
import gzip
import hashlib
import re
import threading

import requests
from flask import Response, request, stream_with_context

## @brief Request headers passed on to koboldcpp.
_FORWARD_REQ = ("content-type", "accept", "accept-language", "range", "cache-control", "last-event-id")
## @brief Response headers passed back from koboldcpp.
_FORWARD_RESP = ("content-type", "content-encoding", "content-length", "cache-control", "etag",
                 "last-modified", "content-range", "accept-ranges", "content-disposition")
_HEAD = re.compile(rb"<head[^>]*>", re.I)

## @brief Per UI: where it lives upstream, what is refused, what only admins may use.
UIS = {
    "lcpp": {"upstream": "/lcpp/",
             "deny": ("mcp", "cors-proxy", "tools", "api/extra/shutdown", "api/admin/",
                      "api/extra/data/", "api/extra/multiplayer", "api/extra/websearch", "websearch"),
             "admin": ("models/load", "models/unload", "api/admin/")},
    "lite": {"upstream": "/",
             "deny": ("mcp", "cors-proxy", "lcpp", "api/extra/shutdown", "api/admin/",
                      "api/extra/data/", "api/extra/multiplayer", "api/extra/websearch",
                      "websearch", "api/extra/preloadstory"),
             "admin": ("api/admin/", "sdapi/v1/options", "models/load", "models/unload")},
}


def _matches(sub, prefixes):
    """! @brief `sub` is one of the paths, or below one of them."""
    s = sub.strip("/").lower()
    return any(s == q or s.startswith(q + "/") for q in (p.strip("/") for p in prefixes))


class Proxy:
    """! @brief Forwarding for one UI.
    @param ui        "lcpp" or "lite"
    @param prefix    the app path it is mounted at ("/chat")
    @param base_url  fn() -> "http://127.0.0.1:5001"
    @param is_admin  fn() -> bool for the requester
    """

    def __init__(self, ui, prefix, base_url, is_admin, shim_tags):
        """! @brief Remember the mount and its rules; patched pages are cached here."""
        self.ui = ui
        self.spec = UIS[ui]
        self.prefix = prefix.rstrip("/")
        self.base_url = base_url
        self.is_admin = is_admin
        self.shim_tags = shim_tags
        self._html = {}  # sha1 of koboldcpp's page -> (plain, gzipped) patched page
        self._lock = threading.Lock()

    def refused(self, sub):
        """! @brief Why this sub-path may not be proxied for the requester, or None."""
        # dot segments would be resolved upstream and walk out of the mount (and the rules)
        if "\\" in sub or "%" in sub or any(p in (".", "..") for p in sub.split("/")):
            return "invalid path"
        if _matches(sub, self.spec["admin"]):
            return None if self.is_admin() else "admin only"
        if _matches(sub, self.spec["deny"]):
            return "not available through the library chat"
        return None

    def url(self, sub):
        """! @brief koboldcpp's URL for a sub-path of this UI."""
        return self.base_url() + self.spec["upstream"] + sub.lstrip("/")

    def _headers(self):
        """! @brief The browser headers koboldcpp may see (never cookies or Authorization)."""
        return {k: v for k, v in request.headers.items() if k.lower() in _FORWARD_REQ}

    def send(self, method, sub, *, json_body=None, data=None, stream=False, params=None, headers=None):
        """! @brief One request to koboldcpp (raises requests.RequestException when it is down)."""
        h = self._headers() if headers is None else headers
        return requests.request(method, self.url(sub), params=params, json=json_body, data=data,
                                headers=h, stream=stream, timeout=(5, 900), allow_redirects=False)

    def forward(self, sub):
        """! @brief Pass the current request through, streaming the reply back unchanged."""
        h = self._headers()
        h["Accept-Encoding"] = request.headers.get("Accept-Encoding", "identity")
        r = self.send(request.method, sub, data=request.get_data(), stream=True,
                      params=request.args.to_dict(flat=False), headers=h)
        out = {k: v for k, v in r.headers.items() if k.lower() in _FORWARD_RESP}
        if r.is_redirect:
            loc = r.headers.get("location", "")
            up = self.spec["upstream"]
            if loc.startswith(up):
                out["Location"] = self.prefix + "/" + loc[len(up):]
            elif loc.startswith("/"):
                out["Location"] = self.prefix + loc
            r.close()
            return Response(b"", r.status_code, out)

        def body():
            """! @brief Stream the reply bytes as they come, closing the upstream at the end."""
            try:
                yield from r.raw.stream(64 * 1024, decode_content=False)
            finally:
                r.close()
        return Response(stream_with_context(body()), r.status_code, out, direct_passthrough=True)

    def page(self, sub=""):
        """! @brief The UI's HTML with the shim injected (patched once per koboldcpp build)."""
        r = self.send("GET", sub, headers={"Accept": "text/html", "Accept-Encoding": "gzip"})
        if r.status_code != 200 or "html" not in (r.headers.get("content-type") or ""):
            return Response(r.content, r.status_code,
                            {k: v for k, v in r.headers.items() if k.lower() in _FORWARD_RESP})
        raw = r.content  # requests has already undone any gzip
        key = hashlib.sha1(raw).hexdigest()
        with self._lock:
            hit = self._html.get(key)
            if hit is None:
                html = raw
                if self.ui == "lcpp":
                    # the SvelteKit bootstrap names its asset root; point it at our mount
                    html = re.sub(rb'assets:\s*"/lcpp"', b'assets: "' + self.prefix.encode() + b'"', html)
                m = _HEAD.search(html)
                tags = self.shim_tags(self.prefix).encode()
                html = html[:m.end()] + tags + html[m.end():] if m else tags + html
                hit = (html, gzip.compress(html, 6))
                self._html = {key: hit}
        plain, packed = hit
        headers = {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store",
                   "Vary": "Accept-Encoding"}
        if "gzip" in (request.headers.get("Accept-Encoding") or "").lower():
            headers["Content-Encoding"] = "gzip"
            return Response(packed, 200, headers)
        return Response(plain, 200, headers)