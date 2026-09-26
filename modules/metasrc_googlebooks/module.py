"""Google Books — book metadata source (key optional)."""
MANIFEST = {
    "id": "metasrc_googlebooks", "name": "Google Books (books)", "version": "1.0.0",
    "description": "Book metadata from the Google Books API. An API key is optional "
                   "(raises the quota).",
    "core": False, "requires": ["metasrc"], "pip": [], "assets": [],
}


def register(host):
    reg = host.get_service("metasrc")
    host.add_config_key("metasrc_googlebooks_key", default="")
    host.add_settings_field(key="metasrc_googlebooks_key", label="Google Books API key (optional)",
                            kind="text", pane="module")

    def search(q):
        params = {"q": f"isbn:{q['isbn']}" if q.get("isbn") else q["q"], "maxResults": 8}
        if host.config.get("metasrc_googlebooks_key"):
            params["key"] = host.config["metasrc_googlebooks_key"]
        out = []
        for it in reg.http_json("https://www.googleapis.com/books/v1/volumes", params).get("items", []):
            v = it.get("volumeInfo", {})
            ids = {x["type"]: x["identifier"] for x in v.get("industryIdentifiers", [])}
            out.append({"id": it["id"], "title": v.get("title", ""),
                        "subtitle": ", ".join(v.get("authors") or []) +
                                    (f" · {v['publishedDate'][:4]}" if v.get("publishedDate") else ""),
                        "thumb": (v.get("imageLinks") or {}).get("thumbnail"),
                        "fields": {"title": v.get("title", ""), "authors": v.get("authors") or [],
                                   "publisher": v.get("publisher", ""), "published": v.get("publishedDate", ""),
                                   "language": v.get("language", ""),
                                   "isbn": ids.get("ISBN_13") or ids.get("ISBN_10", ""),
                                   "description": v.get("description", ""),
                                   "subjects": v.get("categories") or [],
                                   "identifiers": {"google": it["id"]}}})
        return out

    reg.register({"id": "googlebooks", "label": "Google Books", "kind": "book", "priority": 5,
                  "search": search})
