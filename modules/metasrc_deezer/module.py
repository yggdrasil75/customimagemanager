"""Deezer — music metadata source (no key)."""
MANIFEST = {
    "id": "metasrc_deezer", "name": "Deezer (music)", "version": "1.0.0",
    "description": "Track metadata from api.deezer.com: title, artist, album, track/disc "
                   "number, release year, genre.",
    "core": False, "requires": ["metasrc"], "pip": [], "assets": [],
}
_API = "https://api.deezer.com"


def register(host):
    reg = host.get_service("metasrc")

    def search(q):
        out = []
        for t in reg.http_json(f"{_API}/search", {"q": q["q"], "limit": 8}).get("data", []):
            out.append({"id": t["id"], "title": t.get("title", ""),
                        "subtitle": " · ".join(x for x in ((t.get("artist") or {}).get("name"),
                                                          (t.get("album") or {}).get("title")) if x),
                        "thumb": (t.get("album") or {}).get("cover_medium"),
                        "fields": {"title": t.get("title", ""), "artist": (t.get("artist") or {}).get("name", ""),
                                   "album": (t.get("album") or {}).get("title", "")}})
        return out

    def detail(tid):
        t = reg.http_json(f"{_API}/track/{tid}")
        alb = t.get("album") or {}
        genre = ""
        if alb.get("id"):
            a = reg.http_json(f"{_API}/album/{alb['id']}")
            genre = ", ".join(g["name"] for g in (a.get("genres") or {}).get("data", [])[:3])
            alb = a
        return {"track": t.get("track_position"), "disc": t.get("disk_number"),
                "year": (t.get("release_date") or "")[:4],
                "albumartist": (alb.get("artist") or {}).get("name", ""), "genre": genre,
                "composer": ", ".join((t.get("contributors_composer") or []))}

    reg.register({"id": "deezer", "label": "Deezer", "kind": "music", "priority": 5,
                  "search": search, "detail": detail})
