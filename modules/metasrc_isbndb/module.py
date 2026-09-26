"""ISBNdb — book metadata source (API key required)."""
MANIFEST = {
    "id": "metasrc_isbndb", "name": "ISBNdb (books)", "version": "1.0.0",
    "description": "Book metadata from isbndb.com. Needs an ISBNdb API key.",
    "core": False, "requires": ["metasrc"], "pip": [], "assets": [],
}


def register(host):
    reg = host.get_service("metasrc")
    host.add_config_key("metasrc_isbndb_key", default="")
    host.add_settings_field(key="metasrc_isbndb_key", label="ISBNdb API key", kind="text", pane="module")

    def _hdr():
        return {"Authorization": host.config.get("metasrc_isbndb_key", "")}

    def _cand(b):
        return {"id": b.get("isbn13") or b.get("isbn", ""), "title": b.get("title", ""),
                "subtitle": ", ".join(b.get("authors") or []) +
                            (f" · {b['date_published'][:4]}" if b.get("date_published") else ""),
                "thumb": b.get("image"),
                "fields": {"title": b.get("title", ""), "authors": b.get("authors") or [],
                           "publisher": b.get("publisher", ""), "published": str(b.get("date_published") or ""),
                           "language": b.get("language", ""), "isbn": b.get("isbn13") or b.get("isbn", ""),
                           "description": b.get("synopsis", ""), "subjects": b.get("subjects") or []}}

    def search(q):
        if q.get("isbn"):
            return [_cand(reg.http_json(f"https://api2.isbndb.com/book/{q['isbn']}", headers=_hdr())["book"])]
        r = reg.http_json(f"https://api2.isbndb.com/books/{q['q']}", {"pageSize": 8}, headers=_hdr())
        return [_cand(b) for b in r.get("books", [])]

    reg.register({"id": "isbndb", "label": "ISBNdb", "kind": "book",
                  "available": lambda: bool(host.config.get("metasrc_isbndb_key")), "search": search})
