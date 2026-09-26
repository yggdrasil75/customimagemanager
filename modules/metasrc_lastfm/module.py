"""Last.fm — music metadata source (API key required)."""
MANIFEST = {
    "id": "metasrc_lastfm", "name": "Last.fm (music)", "version": "1.0.0",
    "description": "Track metadata from last.fm: title, artist, album, top tags as genre. "
                   "Needs a Last.fm API key.",
    "core": False, "requires": ["metasrc"], "pip": [], "assets": [],
}
_API = "https://ws.audioscrobbler.com/2.0/"


def register(host):
    reg = host.get_service("metasrc")
    host.add_config_key("metasrc_lastfm_key", default="")
    host.add_settings_field(key="metasrc_lastfm_key", label="Last.fm API key", kind="text", pane="module")

    def _call(method, **p):
        p.update(method=method, api_key=host.config.get("metasrc_lastfm_key", ""), format="json")
        return reg.http_json(_API, p)

    def search(q):
        p = {"track": q.get("title") or q["q"], "limit": 8}
        if q.get("artist") and q["q"] == " ".join(x for x in (q.get("artist"), q.get("title")) if x):
            p["artist"] = q["artist"]
        else:
            p["track"] = q["q"]
        r = _call("track.search", **p)
        out = []
        for t in ((r.get("results") or {}).get("trackmatches") or {}).get("track", []):
            out.append({"id": f"{t.get('artist', '')}\t{t.get('name', '')}", "title": t.get("name", ""),
                        "subtitle": t.get("artist", ""),
                        "thumb": next((i["#text"] for i in t.get("image", []) if i.get("size") == "medium"), None),
                        "fields": {"title": t.get("name", ""), "artist": t.get("artist", "")}})
        return out

    def detail(tid):
        artist, _, name = tid.partition("\t")
        t = _call("track.getInfo", artist=artist, track=name).get("track") or {}
        return {"album": (t.get("album") or {}).get("title", ""),
                "albumartist": (t.get("album") or {}).get("artist", ""),
                "genre": ", ".join(x["name"] for x in (t.get("toptags") or {}).get("tag", [])[:3])}

    reg.register({"id": "lastfm", "label": "Last.fm", "kind": "music",
                  "available": lambda: bool(host.config.get("metasrc_lastfm_key")),
                  "search": search, "detail": detail})
