"""SauceNAO — reverse image search for photos (API key required)."""
MANIFEST = {
    "id": "metasrc_saucenao", "name": "SauceNAO (photos)", "version": "1.0.0",
    "description": "Reverse image search: finds where an image came from (Pixiv, Danbooru, "
                   "Twitter, …) and fills artist / source / title. Needs a SauceNAO API key.",
    "core": False, "requires": ["metasrc"], "pip": [], "assets": [],
}


def register(host):
    reg = host.get_service("metasrc")
    host.add_config_key("metasrc_saucenao_key", default="")
    host.add_settings_field(key="metasrc_saucenao_key", label="SauceNAO API key", kind="text", pane="module")

    def search(q):
        if not q.get("abs_path"):
            return []
        with open(q["abs_path"], "rb") as f:
            blob = f.read()
        r = reg.http_multipart("https://saucenao.com/search.php",
                               {"api_key": host.config.get("metasrc_saucenao_key", ""),
                                "output_type": 2, "numres": 8}, {"file": ("image", blob)})
        out = []
        for res in r.get("results", []):
            h, d = res.get("header", {}), res.get("data", {})
            if float(h.get("similarity") or 0) < 60:
                continue
            urls = d.get("ext_urls") or []
            artist = d.get("member_name") or d.get("creator") or d.get("author_name") or ""
            artist = ", ".join(artist) if isinstance(artist, list) else artist
            title = d.get("title") or d.get("source") or d.get("eng_name") or h.get("index_name", "")
            out.append({"id": h.get("index_id"), "title": str(title),
                        "subtitle": f"{h.get('similarity')}% · {h.get('index_name', '')}" + (f" · {artist}" if artist else ""),
                        "thumb": h.get("thumbnail"),
                        "fields": {"artist": artist, "source_url": urls[0] if urls else "",
                                   "description": f"{title} — {urls[0]}" if urls else "",
                                   "tags": [str(d["material"])] if d.get("material") else []}})
        return out

    reg.register({"id": "saucenao", "label": "SauceNAO", "kind": "photo",
                  "available": lambda: bool(host.config.get("metasrc_saucenao_key")), "search": search})
