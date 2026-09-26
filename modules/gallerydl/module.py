"""
gallery-dl fetcher module.
======================================================================
Registers gallery-dl as a FETCHER into the fetch module's registry (found
via host.get_service("fetch")). The generic queue/worker/ingestion lives
in modules/fetch; this module only supplies gallery-dl's fetch operation,
its per-site auth/opts config, and the gallery-dl-specific config UI
endpoints (sites, field discovery, auth). yt-dlp will be an analogous
module that registers its own fetcher and needs none of this.

The fetch contract this satisfies:
    id="gallerydl", label, available(), handles(url), target_key(url)->site,
    fetch(url, tmpdir, on_file), map_meta(meta).
"""

import os
import json

from flask import request, jsonify
from werkzeug.utils import secure_filename

from . import gdl

MANIFEST = {
    "id":          "gallerydl",
    "name":        "gallery-dl fetcher",
    "version":     "1.0.0",
    "description": "Fetch images/galleries from boorus and many sites via "
                   "gallery-dl. Registers as a fetcher in the download queue.",
    "core":        False,
    "requires":    ["fetch"],       # needs the fetch module's registry service
    "pip":         ["gallery-dl"],
    "assets":      ["gdl_sites.js"],
}


def register(host):
    fetch = host.get_service("fetch")
    if fetch is None:
        host.logger.info("gallerydl: fetch service unavailable; not registering")
        return

    cfg = host.config
    # {site: {"fields": [every field ever discovered], "hidden": [...]}}.
    # Discovery only ever ADDS fields (a video post exposes keys an image post
    # doesn't); the user hides what they don't want to see.
    host.add_config_key("gdl_fields", default={})
    host.add_asset("gdl_sites.js")
    host.add_settings_tab("gdl_sites", "Fetch sites", icon="\u2b07")
    _COOKIE_DIR = os.path.join(os.path.dirname(host.media_dir), "gdl_cookies")

    # ── per-site auth -> gallery-dl opt strings ──────────────────────────
    def _write_cookie_file(key, text):
        try:
            os.makedirs(_COOKIE_DIR, exist_ok=True)
            safe = secure_filename(key) or "site"
            path = os.path.join(_COOKIE_DIR, f"{safe}.txt")
            body = text if "\t" in text or text.lstrip().startswith("# ") \
                else _cookiestring_to_netscape(text)
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
            os.chmod(path, 0o600)
            return path
        except Exception as e:
            host.logger.error(f"gdl cookie file write failed for {key}: {e}")
            return ""

    def _cookiestring_to_netscape(s):
        lines = ["# Netscape HTTP Cookie File", "# generated from pasted cookie string"]
        for pair in s.replace("\n", ";").split(";"):
            pair = pair.strip()
            if "=" not in pair:
                continue
            name, _, value = pair.partition("=")
            name, value = name.strip(), value.strip()
            if name:
                lines.append(f".\tTRUE\t/\tFALSE\t2147483647\t{name}\t{value}")
        return "\n".join(lines) + "\n"

    def _compile_auth(site):
        auth = cfg.get("gdl_auth", {}).get(site) or {}
        method = auth.get("method", "none")
        ns = f"extractor.{site}" if site else "extractor"
        opts = []
        if method == "userpass":
            u, p = (auth.get("username") or "").strip(), auth.get("password") or ""
            if u:
                opts += [f"{ns}.username={u}", f"{ns}.password={p}"]
        elif method == "cookies_text":
            text = auth.get("cookies_text") or ""
            if text.strip():
                path = _write_cookie_file(site or "_global", text)
                if path:
                    opts.append(f"{ns}.cookies={json.dumps(path)}")
        elif method == "cookies_browser":
            br = (auth.get("browser") or "").strip()
            if br:
                opts.append(f"{ns}.cookies={json.dumps([br])}")
        return opts

    def _resolve_opts(url):
        all_opts = cfg.get("gdl_opts", {})
        dl_opts = list(all_opts.get("", [])) + _compile_auth("")
        has_site = ({k: v for k, v in all_opts.items() if k} or
                    {k: v for k, v in cfg.get("gdl_auth", {}).items() if k})
        if has_site:
            try:
                cat = gdl.site_of(url, opts=dl_opts)
            except gdl.GdlError:
                cat = ""
            if cat:
                dl_opts += list(all_opts.get(cat, [])) + _compile_auth(cat)
        return dl_opts

    def _auth_public(site):
        a = cfg.get("gdl_auth", {}).get(site) or {}
        return {"method": a.get("method", "none"), "username": a.get("username", ""),
                "browser": a.get("browser", ""),
                "has_password": bool(a.get("password")),
                "has_cookies": bool(a.get("cookies_text"))}

    def _fields_rec(site):
        return cfg.setdefault("gdl_fields", {}).setdefault(
            site, {"fields": [], "hidden": []})

    def _learn_fields(site, fields):
        """Union newly discovered fields into the site's known list (never
        removes). Saves only if something actually changed."""
        if not site:
            return
        rec = _fields_rec(site)
        new = [f for f in fields if f not in rec["fields"]]
        if new or site not in cfg.get("gdl_sites", {}):
            rec["fields"] = sorted(rec["fields"] + new)
            cfg.setdefault("gdl_sites", {}).setdefault(site, {})
            host.save_config()

    def _site_public(site):
        rec = _fields_rec(site)
        return {"site": site, "fields": rec["fields"], "hidden": rec["hidden"],
                "mapping": cfg.get("gdl_sites", {}).get(site, {}),
                "opts": cfg.get("gdl_opts", {}).get(site, []),
                "auth": _auth_public(site)}

    def _known_sites():
        keys = set()
        for k in ("gdl_fields", "gdl_sites", "gdl_opts", "gdl_auth"):
            keys.update(s for s in cfg.get(k, {}) if s)
        return sorted(keys)

    # ── the fetcher ──────────────────────────────────────────────────────
    def _fetch(url, tmpdir, on_file=None):
        # A sidecar that never landed (or a site that omits "category") would
        # otherwise map to no site and lose all metadata: pin the category
        # from the extractor so the saved mapping still applies.
        cat = _target_key(url)
        def _on(mpath, meta):
            meta = dict(meta or {})
            meta.setdefault("category", cat)
            if on_file:
                on_file(mpath, meta)
        return gdl.download(url, tmpdir, opts=_resolve_opts(url), on_file=_on)

    def _target_key(url):
        try:
            return gdl.site_of(url)
        except Exception:
            return ""

    def _map_meta(meta):
        cat = (meta or {}).get("category", "")
        mapping = cfg.get("gdl_sites", {}).get(cat, {})
        return gdl.apply_mapping(meta, mapping)

    fetch.register({
        "id": "gallerydl", "label": "gallery-dl",
        "available": gdl.available,
        # Only claim URLs an extractor actually matches, so hosts served by
        # another fetcher (yt-dlp for YouTube) aren't swallowed here.
        "handles": lambda t: bool(t) and t.strip().startswith(("http://", "https://"))
                             and gdl._find_extractor(t.strip()) is not None,
        "target_key": _target_key,
        "fetch": _fetch,
        "map_meta": _map_meta,
    })

    # ── gallery-dl-specific config endpoints ─────────────────────────────
    m = host.core

    def api_available():
        return jsonify({"available": gdl.available()})

    def api_fields():
        d = request.get_json(force=True, silent=True) or {}
        url = (d.get("url") or "").strip()
        if not url:
            return jsonify({"success": False, "error": "no url"})
        try:
            found = gdl.discover_fields(url, opts=_resolve_opts(url))   # {"site", "fields": [...]}
        except gdl.GdlError as e:
            return jsonify({"success": False, "error": str(e)})
        site = found.get("site") or ""
        _learn_fields(site, found.get("fields") or [])
        # Same shape as /api/gdl/site: `fields` is the site's full known list
        # (this discovery unioned with every earlier one), plus `hidden`.
        return jsonify({"success": True, **_site_public(site),
                        "new_fields": found.get("fields") or []})

    def api_config():
        if request.method == "GET":
            return jsonify({"success": True,
                            "sites": cfg.get("gdl_sites", {}),
                            "fields": cfg.get("gdl_fields", {}),
                            "opts": cfg.get("gdl_opts", {}),
                            "auth": {s: _auth_public(s) for s in cfg.get("gdl_auth", {})}})
        d = request.get_json(force=True, silent=True) or {}
        # Two shapes: the modal/settings tab save ONE site — {site,
        # auth:{method,…}, mapping:{…}, hidden:[…], opts:"one per line"} —
        # while a whole-config client sends the maps {sites:{site:mapping},
        # opts:{site:[…]}, auth:{site:blob}}.
        site = (d.get("site") or "").strip()
        if "sites" in d:
            cfg["gdl_sites"] = d["sites"] or {}
        if "mapping" in d and site:
            # MERGE: only fields the client mentions change ("ignore" drops
            # one). Fields it didn't render (hidden, or not shown by this
            # post) keep their saved target instead of being wiped.
            cur = cfg.setdefault("gdl_sites", {}).setdefault(site, {})
            for f, t in (d["mapping"] or {}).items():
                if not t or t == "ignore":
                    cur.pop(f, None)
                else:
                    cur[f] = t
        if "hidden" in d and site:
            _fields_rec(site)["hidden"] = [str(f) for f in (d["hidden"] or [])]
        if d.get("forget") and site:
            for k in ("gdl_fields", "gdl_sites", "gdl_opts", "gdl_auth"):
                cfg.get(k, {}).pop(site, None)
            host.save_config()
            return jsonify({"success": True})
        if "opts" in d:
            if isinstance(d["opts"], dict):
                cfg["gdl_opts"] = d["opts"]
            elif site:                                   # textarea: one option per line
                lines = d["opts"] if isinstance(d["opts"], list) else str(d["opts"] or "").splitlines()
                cfg.setdefault("gdl_opts", {})[site] = [l.strip() for l in lines if l.strip()]
        if "auth" in d:
            auth = d["auth"] or {}
            per_site = {site: auth} if site and "method" in auth else auth   # flat blob vs {site: blob}
            # merge, preserving secrets when the client sends redacted blobs
            existing = cfg.get("gdl_auth", {})
            for s_, blob in per_site.items():
                if not isinstance(blob, dict):
                    continue
                cur = dict(existing.get(s_, {}))
                cur.update({k: v for k, v in blob.items()
                            if k not in ("has_password", "has_cookies")})
                existing[s_] = cur
            cfg["gdl_auth"] = existing
        host.save_config()
        return jsonify({"success": True})

    def api_site():
        url = (request.get_json(force=True, silent=True) or {}).get("url", "").strip()
        if not url:
            return jsonify({"success": False, "error": "url required"}), 400
        try:
            site = gdl.site_of(url) or ""
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 400
        if not site:
            return jsonify({"success": False,
                            "error": "No gallery-dl extractor matches that URL."}), 400
        _learn_fields(site, [])          # a URL for a new site registers it
        return jsonify({"success": True, **_site_public(site)})

    def api_sites():
        """Every site we know anything about, for the settings tab's list."""
        if request.method == "POST":     # {site} -> full record
            site = (request.get_json(force=True, silent=True) or {}).get("site", "").strip()
            if not site:
                return jsonify({"success": False, "error": "site required"}), 400
            return jsonify({"success": True, **_site_public(site)})
        return jsonify({"success": True, "sites": [
            {"site": s, "fields": len(_fields_rec(s)["fields"]),
             "mapped": len(cfg.get("gdl_sites", {}).get(s, {})),
             "auth": _auth_public(s)["method"]} for s in _known_sites()]})

    def api_targets():
        schemas = host.get_service("metadata_schema") or {}
        exif_groups, xmp_groups = [], []
        try:
            for grp in schemas.get("exif", lambda: {})().get("groups", []):
                tags = [f["name"] for f in grp.get("fields", []) if f.get("writable")]
                if tags:
                    exif_groups.append({"group": grp.get("title") or grp.get("name") or "EXIF",
                                        "tags": tags})
        except Exception as e:
            host.logger.error(f"gdl targets exif: {e}")
        try:
            for ns in schemas.get("xmp", lambda: {})().get("namespaces", []):
                toks = [f"Xmp.{ns['ns']}.{f['name']}" for f in ns.get("fields", [])]
                if toks:
                    xmp_groups.append({"group": ns.get("title") or ns["ns"],
                                       "ns": ns["ns"], "tokens": toks})
        except Exception as e:
            host.logger.error(f"gdl targets xmp: {e}")
        return jsonify({"success": True, "exif_groups": exif_groups,
                        "xmp_groups": xmp_groups})

    host.register_app_modal("gdl_modal.html")
    host.add_route("/api/gdl/site", api_site, methods=["POST"], endpoint="gdl_site",
                   feature="fetch", level="write")
    host.add_route("/api/gdl/sites", api_sites, methods=["GET", "POST"], endpoint="gdl_sites",
                   feature="fetch", level="write")
    host.add_route("/api/gdl/targets", api_targets, endpoint="gdl_targets", feature="fetch")
    host.add_route("/api/gdl/available", api_available, endpoint="gdl_available", feature="fetch")
    host.add_route("/api/gdl/fields", api_fields, methods=["POST"], endpoint="gdl_fields",
                   feature="fetch", level="write")
    host.add_route("/api/gdl/config", api_config, methods=["GET", "POST"], endpoint="gdl_config",
                   feature="fetch", level="write")

    host.logger.info("gallerydl: registered fetcher + config endpoints")
