"""Danbooru — photo tag source (md5 match, or tag search; no key)."""
MANIFEST = {
    "id": "metasrc_danbooru", "name": "Danbooru (photos)", "version": "1.0.0",
    "description": "Tags, artist and source URL for an image found on danbooru.donmai.us "
                   "by md5; the search box doubles as a Danbooru tag query.",
    "core": False, "requires": ["metasrc"], "pip": [], "assets": [],
}
_API = "https://danbooru.donmai.us"


def _cand(p):
    tags = [t for k in ("tag_string_character", "tag_string_copyright", "tag_string_general")
            for t in (p.get(k) or "").split()]
    return {"id": p.get("id"), "title": f"post #{p.get('id')}",
            "subtitle": " · ".join(x for x in (p.get("tag_string_artist"), p.get("tag_string_copyright", "")[:60]) if x),
            "thumb": p.get("preview_file_url"),
            "fields": {"tags": [t.replace("_", " ") for t in tags],
                       "artist": (p.get("tag_string_artist") or "").replace("_", " "),
                       "source_url": p.get("source") or f"{_API}/posts/{p.get('id')}"}}


def register(host):
    reg = host.get_service("metasrc")

    def search(q):
        posts = reg.http_json(f"{_API}/posts.json", {"tags": f"md5:{q['md5']}", "limit": 1}) if q.get("md5") else []
        stem = (q.get("rel_path") or "").rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if not posts and q.get("q") and q["q"] != stem:
            posts = reg.http_json(f"{_API}/posts.json", {"tags": q["q"], "limit": 8})
        return [_cand(p) for p in posts if p.get("id")]

    reg.register({"id": "danbooru", "label": "Danbooru", "kind": "photo", "priority": 10, "search": search})
