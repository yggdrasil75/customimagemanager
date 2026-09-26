"""iTunes Search API — music metadata source (no key)."""
MANIFEST = {
    "id": "metasrc_itunes", "name": "iTunes (music)", "version": "1.0.0",
    "description": "Track metadata from the iTunes Search API: title, artist, album, "
                   "track/disc number, year, genre.",
    "core": False, "requires": ["metasrc"], "pip": [], "assets": [],
}


def register(host):
    reg = host.get_service("metasrc")

    def search(q):
        r = reg.http_json("https://itunes.apple.com/search",
                          {"term": q["q"], "entity": "song", "media": "music", "limit": 8})
        out = []
        for t in r.get("results", []):
            out.append({"id": t.get("trackId"), "title": t.get("trackName", ""),
                        "subtitle": " · ".join(x for x in (t.get("artistName"), t.get("collectionName"),
                                                          (t.get("releaseDate") or "")[:4]) if x),
                        "thumb": t.get("artworkUrl100"),
                        "fields": {"title": t.get("trackName", ""), "artist": t.get("artistName", ""),
                                   "album": t.get("collectionName", ""),
                                   "albumartist": t.get("collectionArtistName") or t.get("artistName", ""),
                                   "track": t.get("trackNumber"), "disc": t.get("discNumber"),
                                   "year": (t.get("releaseDate") or "")[:4],
                                   "genre": t.get("primaryGenreName", "")}})
        return out

    reg.register({"id": "itunes", "label": "iTunes", "kind": "music", "search": search})
