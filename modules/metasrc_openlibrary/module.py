"""Open Library — book metadata source (no key)."""
MANIFEST = {
    "id": "metasrc_openlibrary", "name": "Open Library (books)", "version": "1.0.0",
    "description": "Book metadata from openlibrary.org: title, authors, ISBN, publisher, "
                   "subjects, description, cover.",
    "core": False, "requires": ["metasrc"], "pip": [], "assets": [],
}
_API = "https://openlibrary.org"


def register(host):
    reg = host.get_service("metasrc")

    def search(q):
        params = {"limit": 8, "fields": "key,title,subtitle,author_name,first_publish_year,isbn,"
                                        "publisher,language,subject,cover_i,number_of_pages_median"}
        params["isbn" if q.get("isbn") else "q"] = q["isbn"] or q["q"]
        out = []
        for d in reg.http_json(f"{_API}/search.json", params).get("docs", []):
            isbns = d.get("isbn") or []
            out.append({"id": d["key"], "title": d.get("title", ""),
                        "subtitle": ", ".join(d.get("author_name") or []) +
                                    (f" · {d['first_publish_year']}" if d.get("first_publish_year") else ""),
                        "thumb": f"https://covers.openlibrary.org/b/id/{d['cover_i']}-M.jpg" if d.get("cover_i") else None,
                        "fields": {"title": d.get("title", ""), "authors": d.get("author_name") or [],
                                   "published": str(d.get("first_publish_year") or ""),
                                   "isbn": next((i for i in isbns if len(i) == 13), isbns[0] if isbns else ""),
                                   "publisher": (d.get("publisher") or [""])[0],
                                   "language": (d.get("language") or [""])[0],
                                   "subjects": (d.get("subject") or [])[:20],
                                   "identifiers": {"openlibrary": d["key"].rsplit("/", 1)[-1]}}})
        return out

    def detail(key):                       # works/OL…W → description
        w = reg.http_json(f"{_API}{key}.json")
        desc = w.get("description", "")
        return {"description": desc.get("value", "") if isinstance(desc, dict) else desc}

    reg.register({"id": "openlibrary", "label": "Open Library", "kind": "book", "priority": 10,
                  "search": search, "detail": detail})
