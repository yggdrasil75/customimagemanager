"""Discogs — music metadata source (personal access token required)."""
MANIFEST = {
    "id": "metasrc_discogs", "name": "Discogs (music)", "version": "1.0.0",
    "description": "Release metadata from discogs.com: album, artist, year, genre, label. "
                   "Needs a Discogs personal access token.",
    "core": False, "requires": ["metasrc"], "pip": [], "assets": [],
}


def register(host):
    reg = host.get_service("metasrc")
    host.add_config_key("metasrc_discogs_token", default="")
    host.add_settings_field(key="metasrc_discogs_token", label="Discogs personal access token",
                            kind="text", pane="module")

    def _hdr():
        return {"Authorization": f"Discogs token={host.config.get('metasrc_discogs_token', '')}"}

    def search(q):
        params = {"type": "release", "per_page": 8}
        if q.get("artist") or q.get("album") or q.get("title"):
            params.update({k: v for k, v in (("artist", q.get("artist")), ("release_title", q.get("album")),
                                             ("track", q.get("title"))) if v})
        if q.get("q") and q["q"] != " ".join(x for x in (q.get("artist"), q.get("title")) if x):
            params = {"type": "release", "per_page": 8, "q": q["q"]}
        out = []
        for r in reg.http_json("https://api.discogs.com/database/search", params, headers=_hdr()).get("results", []):
            artist, _, album = r.get("title", "").partition(" - ")
            out.append({"id": r.get("id"), "title": album or r.get("title", ""),
                        "subtitle": " · ".join(x for x in (artist, str(r.get("year") or ""),
                                                          ", ".join(r.get("label") or [])[:40]) if x),
                        "thumb": r.get("thumb"),
                        "fields": {"album": album or r.get("title", ""), "albumartist": artist,
                                   "artist": artist, "year": str(r.get("year") or ""),
                                   "genre": ", ".join((r.get("style") or r.get("genre") or [])[:3])}})
        return out

    def detail(rid):                       # track number + title from the tracklist
        rel = reg.http_json(f"https://api.discogs.com/releases/{rid}", headers=_hdr())
        return {"year": str(rel.get("year") or ""), "genre": ", ".join((rel.get("styles") or rel.get("genres") or [])[:3])}

    reg.register({"id": "discogs", "label": "Discogs", "kind": "music",
                  "available": lambda: bool(host.config.get("metasrc_discogs_token")),
                  "search": search, "detail": detail})
