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
    "assets":      [],
}


def register(host):
    fetch = host.get_service("fetch")
    if fetch is None:
        host.logger.info("gallerydl: fetch service unavailable; not registering")
        return

    cfg = host.config
    _COOKIE_DIR = os.path.join(os.path.dirname(host.media_dir), "gdl_cookies")

    # ── per-site auth -> gallery-dl opt strings ──────────────────────────
    def _write_cookie_file(key, text):
        from werkzeug.utils import secure_filename
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

    # ── the fetcher ──────────────────────────────────────────────────────
    def _fetch(url, tmpdir, on_file=None):
        return gdl.download(url, tmpdir, opts=_resolve_opts(url), on_file=on_file)

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
        "handles": lambda t: bool(t) and t.strip().startswith(("http://", "https://")),
        "target_key": _target_key,
        "fetch": _fetch,
        "map_meta": _map_meta,
    })

    # ── gallery-dl-specific config endpoints ─────────────────────────────
    from flask import request, jsonify
    def _mgr():
        import manager as m
        return m
    auth = _mgr()._auth

    def api_available():
        return jsonify({"available": gdl.available()})

    def api_fields():
        d = request.get_json(force=True, silent=True) or {}
        url = (d.get("url") or "").strip()
        if not url:
            return jsonify({"success": False, "error": "no url"})
        try:
            return jsonify({"success": True,
                            "fields": gdl.discover_fields(url, opts=_resolve_opts(url))})
        except gdl.GdlError as e:
            return jsonify({"success": False, "error": str(e)})

    def api_config():
        if request.method == "GET":
            return jsonify({"success": True,
                            "sites": cfg.get("gdl_sites", {}),
                            "opts": cfg.get("gdl_opts", {}),
                            "auth": {s: _auth_public(s) for s in cfg.get("gdl_auth", {})}})
        d = request.get_json(force=True, silent=True) or {}
        m = _mgr()
        if "sites" in d: cfg["gdl_sites"] = d["sites"]
        if "opts" in d: cfg["gdl_opts"] = d["opts"]
        if "auth" in d:
            # merge, preserving secrets when the client sends redacted blobs
            existing = cfg.get("gdl_auth", {})
            for site, blob in (d["auth"] or {}).items():
                cur = dict(existing.get(site, {}))
                cur.update({k: v for k, v in blob.items()
                            if k not in ("has_password", "has_cookies")})
                existing[site] = cur
            cfg["gdl_auth"] = existing
        m.save_config()
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
        return jsonify({"success": True, "site": site,
                        "mapping": cfg.get("gdl_sites", {}).get(site, {}),
                        "opts": cfg.get("gdl_opts", {}).get(site, []),
                        "auth": _auth_public(site)})

    def api_targets():
        import exif_fields, xmp_fields
        exif_groups, xmp_groups = [], []
        try:
            for grp in exif_fields.schema_dict().get("groups", []):
                tags = [f["name"] for f in grp.get("fields", []) if f.get("writable")]
                if tags:
                    exif_groups.append({"group": grp.get("title") or grp.get("name") or "EXIF",
                                        "tags": tags})
        except Exception as e:
            host.logger.error(f"gdl targets exif: {e}")
        try:
            for ns in xmp_fields.schema_dict().get("namespaces", []):
                toks = [f"Xmp.{ns['ns']}.{f['name']}" for f in ns.get("fields", [])]
                if toks:
                    xmp_groups.append({"group": ns.get("title") or ns["ns"],
                                       "ns": ns["ns"], "tokens": toks})
        except Exception as e:
            host.logger.error(f"gdl targets xmp: {e}")
        return jsonify({"success": True, "exif_groups": exif_groups,
                        "xmp_groups": xmp_groups})

    host.add_route("/api/gdl/site",
                   auth.require_feature("fetch", level="write")(api_site),
                   methods=["POST"], endpoint="gdl_site")
    host.add_route("/api/gdl/targets", auth.require_feature("fetch")(api_targets),
                   endpoint="gdl_targets")
    host.add_route("/api/gdl/available", auth.require_feature("fetch")(api_available),
                   endpoint="gdl_available")
    host.add_route("/api/gdl/fields",
                   auth.require_feature("fetch", level="write")(api_fields),
                   methods=["POST"], endpoint="gdl_fields")
    host.add_route("/api/gdl/config",
                   auth.require_feature("fetch", level="write")(api_config),
                   methods=["GET", "POST"], endpoint="gdl_config")

    host.logger.info("gallerydl: registered fetcher + config endpoints")
