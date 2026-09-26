"""e621 — photo tag source (md5 match, or tag search; no key)."""
MANIFEST = {
    "id": "metasrc_e621", "name": "e621 (photos)", "version": "1.0.0",
    "description": "Tags, artist and source URL for an image found on e621.net by md5; "
                   "the search box doubles as a tag query.",
    "core": False, "requires": ["metasrc"], "pip": [], "assets": [],
}
_API = "https://e621.net"


def register(host):
    reg = host.get_service("metasrc")

    def _posts(tags, limit):
        return reg.http_json(f"{_API}/posts.json", {"tags": tags, "limit": limit}).get("posts", [])

    def search(q):
        posts = _posts(f"md5:{q['md5']}", 1) if q.get("md5") else []
        stem = (q.get("rel_path") or "").rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if not posts and q.get("q") and q["q"] != stem:
            posts = _posts(q["q"], 8)
        out = []
        for p in posts:
            t = p.get("tags") or {}
            tags = [x for k in ("character", "copyright", "species", "general") for x in t.get(k, [])]
            out.append({"id": p.get("id"), "title": f"post #{p.get('id')}",
                        "subtitle": ", ".join(t.get("artist", [])[:3]),
                        "thumb": (p.get("preview") or {}).get("url"),
                        "fields": {"tags": [x.replace("_", " ") for x in tags],
                                   "artist": ", ".join(t.get("artist", [])).replace("_", " "),
                                   "source_url": (p.get("sources") or [f"{_API}/posts/{p.get('id')}"])[0]}})
        return out

    reg.register({"id": "e621", "label": "e621", "kind": "photo", "search": search})
