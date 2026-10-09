"""! @file
@brief Nominatim (OpenStreetMap) - online reverse geocoding for photos with GPS (no key).

A candidate carries the place as the photo place fields (city, state, country,
country_code, and the finer place name as location); the metasrc hub writes
them to photoshop:City / State / Country and Iptc4xmpCore:CountryCode /
Location - the same homes the map module fills offline - only where the file
has none, unless the user applies with "overwrite". The map module's places
row follows the write.
"""
MANIFEST = {
    "id": "metasrc_nominatim", "name": "Nominatim / OSM (photos)", "version": "1.1.0",
    "description": "Turns a photo's GPS position into its place (city, region, country, "
                 "place name) and a location description via OpenStreetMap.",
    "core": False, "requires": ["metasrc"], "pip": [], "assets": [],
}

## @brief Address keys naming the town, finest first.
_TOWN = ("city", "town", "village", "hamlet", "municipality")
## @brief Address keys naming something finer than the town (a landmark, a district).
_FINER = ("tourism", "amenity", "leisure", "building", "historic", "neighbourhood", "suburb", "quarter")


def place_fields(r):
    """! @brief A Nominatim reverse result as photo place fields (only the ones it has)."""
    a = r.get("address") or {}
    town = next((a[k] for k in _TOWN if a.get(k)), "")
    finer = r.get("name") or next((a[k] for k in _FINER if a.get(k)), "")
    out = {"city": town, "state": a.get("state") or a.get("region") or "",
           "country": a.get("country") or "", "country_code": str(a.get("country_code") or "").upper(),
           "location": finer if finer and finer != town else ""}
    return {k: v for k, v in out.items() if v}


def register(host):
    """! @brief Register the source with the metasrc hub."""
    reg = host.get_service("metasrc")

    def search(q):
        """! @brief One reverse lookup of the photo's GPS position."""
        if q.get("lat") is None:
            return []
        r = reg.http_json("https://nominatim.openstreetmap.org/reverse",
                          {"lat": q["lat"], "lon": q["lon"], "format": "jsonv2", "zoom": 18})
        fields = place_fields(r)
        parts = [fields.get(k) for k in ("location", "city", "country") if fields.get(k)]
        fields.update(description=r.get("display_name", ""),
                      source_url=f"https://www.openstreetmap.org/?mlat={q['lat']}&mlon={q['lon']}")
        return [{"id": r.get("place_id"), "title": ", ".join(parts[:2]) or "location",
                 "subtitle": r.get("display_name", ""), "thumb": None, "fields": fields}]

    reg.register({"id": "nominatim", "label": "Nominatim (OSM)", "kind": "photo", "search": search})
