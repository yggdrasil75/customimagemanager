"""! @file
@brief assets.py: the page's scripts and the modules' assets become content-hashed,
gzip-precompressed bundles that the app serves immutable; --no-bundle serves
the files one by one."""
import gzip
import logging
import os
import assets


def test_bundle_concatenates_in_order_and_hashes(tmp_path, monkeypatch):
    monkeypatch.setattr(assets, "BUILD_DIR", str(tmp_path / "build"))
    monkeypatch.setattr(assets, "ROOT", str(tmp_path))
    (tmp_path / "static").mkdir()
    (tmp_path / "static" / "a.js").write_text("var a = 1")          # no trailing ';'
    (tmp_path / "static" / "b.js").write_text("var b = a + 1;\n")
    monkeypatch.setattr(assets, "CORE_JS", ["a.js", "b.js", "missing.js"])
    m = tmp_path / "modules" / "x" / "static"
    m.mkdir(parents=True)
    (m / "x.js").write_text("var x = b;")
    (m / "x.css").write_text(".x { color: red; }")
    mods = [{"url": "/modules/x/static/x.css", "kind": "css", "module_id": "x", "path": str(m / "x.css")},
            {"url": "/modules/x/static/x.js", "kind": "js", "module_id": "x", "path": str(m / "x.js")}]
    out = assets.build(mods, logging.getLogger("t"))
    core = tmp_path / "build" / os.path.basename(out["core_js"])
    text = core.read_text()
    assert text.index("var a") < text.index("var b") and ";" in text.split("var b")[0]
    assert gzip.decompress(open(str(core) + ".gz", "rb").read()).decode() == text
    assert out["modules_js"].startswith("/static/build/modules.") and out["modules_css"].endswith(".css")
    # same content -> same name; a change -> a new name and the old one is dropped
    again = assets.build(mods, logging.getLogger("t"))
    assert again == out
    (tmp_path / "static" / "a.js").write_text("var a = 2")
    changed = assets.build(mods, logging.getLogger("t"))
    assert changed["core_js"] != out["core_js"] and not core.exists()


def test_app_serves_bundles_immutable_and_gzipped(client, app, monkeypatch):
    app._build_bundles()
    try:
        assert app._bundles.get("core_js")
        html = client.get("/").get_data(as_text=True)
        assert app._bundles["core_js"] in html and "/static/globals.js" not in html
        r = client.get(app._bundles["core_js"], headers={"Accept-Encoding": "gzip"})
        assert r.status_code == 200 and r.headers["Content-Encoding"] == "gzip"
        assert "immutable" in r.headers["Cache-Control"]
        assert b"awaitBulk" in gzip.decompress(r.data)
        plain = client.get(app._bundles["core_js"])
        assert "Content-Encoding" not in plain.headers and b"awaitBulk" in plain.data
        urls = [a["url"] for a in client.get("/api/module_assets").get_json()["assets"]]
        assert all(u.startswith("/static/build/") for u in urls) and len(urls) <= 2
        assert client.get("/static/build/nope.js").status_code == 404
    finally:
        app._bundles = {}
    assert "/static/globals.js" in client.get("/").get_data(as_text=True)