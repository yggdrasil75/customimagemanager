"""Gelbooru — photo tag source (md5 match, or tag search; key optional)."""
MANIFEST = {
    "id": "metasrc_gelbooru", "name": "Gelbooru (photos)", "version": "1.0.0",
    "description": "Tags and source URL for an image found on gelbooru.com by md5; the "
                   "search box doubles as a tag query. API key + user id optional.",
    "core": False, "requires": ["metasrc"], "pip": [], "assets": [],
}
_API = "https://gelbooru.com/index.php"


def register(host):
    reg = host.get_service("metasrc")
    host.add_config_key("metasrc_gelbooru_key", default="")
    host.add_config_key("metasrc_gelbooru_user", default="")
    host.add_settings_field(key="metasrc_gelbooru_key", label="Gelbooru API key (optional)", kind="text", pane="module")
    host.add_settings_field(key="metasrc_gelbooru_user", label="Gelbooru user id (optional)", kind="text", pane="module")

    def _posts(tags, limit):
        p = {"page": "dapi", "s": "post", "q": "index", "json": 1, "tags": tags, "limit": limit}
        if host.config.get("metasrc_gelbooru_key"):
            p.update(api_key=host.config["metasrc_gelbooru_key"], user_id=host.config.get("metasrc_gelbooru_user", ""))
        r = reg.http_json(_API, p) or {}
        return r.get("post", []) if isinstance(r, dict) else r

    def search(q):
        posts = _posts(f"md5:{q['md5']}", 1) if q.get("md5") else []
        stem = (q.get("rel_path") or "").rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if not posts and q.get("q") and q["q"] != stem:
            posts = _posts(q["q"], 8)
        return [{"id": p.get("id"), "title": f"post #{p.get('id')}", "subtitle": (p.get("tags") or "")[:80],
                 "thumb": p.get("preview_url"),
                 "fields": {"tags": [t.replace("_", " ") for t in (p.get("tags") or "").split()],
                            "source_url": p.get("source") or f"{_API}?page=post&s=view&id={p.get('id')}"}}
                for p in posts if p.get("id")]

    reg.register({"id": "gelbooru", "label": "Gelbooru", "kind": "photo", "search": search})
