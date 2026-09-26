"""MusicBrainz — music metadata source (no key)."""
MANIFEST = {
    "id": "metasrc_musicbrainz", "name": "MusicBrainz (music)", "version": "1.0.0",
    "description": "Track metadata from musicbrainz.org: title, artist, album, year, "
                   "track/disc number, genre tags.",
    "core": False, "requires": ["metasrc"], "pip": [], "assets": [],
}


def register(host):
    reg = host.get_service("metasrc")

    def search(q):
        if q.get("q") and q["q"] != " ".join(x for x in (q.get("artist"), q.get("title")) if x):
            lucene = q["q"]
        else:
            lucene = " AND ".join(f'{k}:"{v}"' for k, v in (("recording", q.get("title")),
                                                            ("artist", q.get("artist")),
                                                            ("release", q.get("album"))) if v)
        r = reg.http_json("https://musicbrainz.org/ws/2/recording", {"query": lucene, "fmt": "json", "limit": 8})
        out = []
        for rec in r.get("recordings", []):
            artist = ", ".join(a.get("name", "") for a in rec.get("artist-credit", []) if isinstance(a, dict))
            rel = (rec.get("releases") or [{}])[0]
            med = (rel.get("media") or [{}])[0]
            trk = (med.get("track") or [{}])[0]
            out.append({"id": rec["id"], "title": rec.get("title", ""),
                        "subtitle": " · ".join(x for x in (artist, rel.get("title"), (rel.get("date") or "")[:4]) if x),
                        "thumb": f"https://coverartarchive.org/release/{rel['id']}/front-250" if rel.get("id") else None,
                        "fields": {"title": rec.get("title", ""), "artist": artist, "album": rel.get("title", ""),
                                   "albumartist": ", ".join(a.get("name", "") for a in rel.get("artist-credit", [])
                                                            if isinstance(a, dict)),
                                   "year": (rel.get("date") or "")[:4], "track": trk.get("number"),
                                   "disc": med.get("position"),
                                   "genre": ", ".join(t["name"] for t in (rec.get("tags") or [])[:3])}})
        return out

    reg.register({"id": "musicbrainz", "label": "MusicBrainz", "kind": "music", "priority": 10,
                  "search": search})
