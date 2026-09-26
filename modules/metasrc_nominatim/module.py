"""Nominatim (OpenStreetMap) — reverse geocoding for photos with GPS (no key)."""
MANIFEST = {
    "id": "metasrc_nominatim", "name": "Nominatim / OSM (photos)", "version": "1.0.0",
    "description": "Turns a photo's EXIF GPS position into place tags (city, region, "
                   "country) and a location description via OpenStreetMap.",
    "core": False, "requires": ["metasrc"], "pip": [], "assets": [],
}


def register(host):
    reg = host.get_service("metasrc")

    def search(q):
        if q.get("lat") is None:
            return []
        r = reg.http_json("https://nominatim.openstreetmap.org/reverse",
                          {"lat": q["lat"], "lon": q["lon"], "format": "jsonv2", "zoom": 16})
        a = r.get("address") or {}
        place = [a.get(k) for k in ("tourism", "amenity", "suburb", "city", "town", "village",
                                    "state", "country") if a.get(k)]
        return [{"id": r.get("place_id"), "title": r.get("name") or ", ".join(place[:2]) or "location",
                 "subtitle": r.get("display_name", ""), "thumb": None,
                 "fields": {"tags": list(dict.fromkeys(place)), "description": r.get("display_name", ""),
                            "source_url": f"https://www.openstreetmap.org/?mlat={q['lat']}&mlon={q['lon']}"}}]

    reg.register({"id": "nominatim", "label": "Nominatim (OSM)", "kind": "photo", "search": search})
