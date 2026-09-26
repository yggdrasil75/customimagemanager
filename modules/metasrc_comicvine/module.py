"""Comic Vine — comic/volume metadata source (API key required)."""
import re

MANIFEST = {
    "id": "metasrc_comicvine", "name": "Comic Vine (comics)", "version": "1.0.0",
    "description": "Comic volume metadata from comicvine.gamespot.com (publisher, year, "
                   "description). Needs a Comic Vine API key.",
    "core": False, "requires": ["metasrc"], "pip": [], "assets": [],
}
_TAGS = re.compile(r"<[^>]+>")


def register(host):
    reg = host.get_service("metasrc")
    host.add_config_key("metasrc_comicvine_key", default="")
    host.add_settings_field(key="metasrc_comicvine_key", label="Comic Vine API key", kind="text", pane="module")

    def search(q):
        r = reg.http_json("https://comicvine.gamespot.com/api/search/",
                          {"api_key": host.config.get("metasrc_comicvine_key", ""), "format": "json",
                           "resources": "volume", "limit": 8, "query": q["q"]})
        out = []
        for v in r.get("results", []):
            pub = (v.get("publisher") or {}).get("name", "")
            out.append({"id": v.get("id"), "title": v.get("name", ""),
                        "subtitle": " · ".join(x for x in (pub, str(v.get("start_year") or ""),
                                                          f"{v.get('count_of_issues')} issues") if x),
                        "thumb": (v.get("image") or {}).get("small_url"),
                        "fields": {"title": v.get("name", ""), "series": v.get("name", ""), "publisher": pub,
                                   "published": str(v.get("start_year") or ""),
                                   "description": _TAGS.sub("", v.get("description") or v.get("deck") or ""),
                                   "identifiers": {"comicvine": str(v.get("id"))}}})
        return out

    reg.register({"id": "comicvine", "label": "Comic Vine", "kind": "book",
                  "available": lambda: bool(host.config.get("metasrc_comicvine_key")), "search": search})
