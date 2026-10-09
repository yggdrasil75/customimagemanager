"""! @file
@brief Reorganize module: template rendering, preview, run (moves with sidecars and
rows), collisions, undo, skip folders, empty-folder removal."""
import os
from datetime import datetime, timezone

import pytest

from cimtest import read_meta, write_meta
from modules.reorganize import template as tpl

COLS = ("d_actual", "d_original", "d_capture", "d_digitized", "d_modified")
FOLDER = "reorg_src"


def _set_date(host, fn, iso, col="d_original"):
    db = host.db()
    db.execute("UPDATE files SET " + ", ".join(f"{c}=NULL, {c}_epoch=NULL" for c in COLS)
               + " WHERE rel_path=?", (fn,))
    if iso:
        ep = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp()
        db.execute(f"UPDATE files SET {col}=?, {col}_epoch=? WHERE rel_path=?", (iso[:10], ep, fn))
    db.commit()


@pytest.fixture
def templates(host):
    """! @brief Set the image template for a test and restore the previous one after."""
    saved = {k: host.config.get(k) for k in ("reorganize_template_image", "reorganize_skip_folders",
                                             "reorganize_date_fallback_mtime", "reorganize_keep_owner_tree")}

    def _set(**kw):
        for k, v in kw.items():
            host.set_config("reorganize_" + k, v, save=False)
    yield _set
    for k, v in saved.items():
        host.set_config(k, v, save=False)


def _post(client, url, body):
    r = client.post(url, json=body)
    j = r.get_json()
    assert r.status_code == 200, (r.status_code, j)
    assert j["success"], j
    return j


def _media(*parts):
    return os.path.join("media", *parts)


def _cleanup(client, names):
    for n in names:
        client.post("/api/delete", json={"filename": n})


# -- template unit tests -----------------------------------------------------------
CTX = {"kind": "image", "year": "2021", "month": "03", "day": "04", "date": "2021-03-04",
       "folder": "a/b", "top": "a", "owner": "", "album": "Trip", "albums": "Trip, Zoo",
       "person": "Ann", "tag": "cat", "tag_prefix": lambda p: "Bob" if p == "artist:" else "",
       "make": "Canon", "model": "EOS R5", "camera": "Canon EOS R5", "rating": "4",
       "artist": "", "title": "", "genre": "", "series": "", "name": "pic", "ext": ".jpg",
       "sha8": "deadbeef"}


def test_render_tokens_keep_name():
    assert tpl.render("{year}/{year}-{month}/{album}", CTX) == "2021/2021-03/Trip/pic.jpg"
    assert tpl.render("{camera}/{person}", CTX) == "Canon EOS R5/Ann/pic.jpg"
    assert tpl.render("{tag:artist:}/{sha8}", CTX) == "Bob/deadbeef/pic.jpg"


def test_render_name_ext_filters_and_defaults():
    assert tpl.render("{kind|upper}/{name}_{sha8}{ext}", CTX) == "IMAGE/pic_deadbeef.jpg"
    assert tpl.render("x/{album|slug}-{name}", CTX) == "x/trip-pic.jpg"      # ext appended
    ctx = dict(CTX, year="", month="", album="")
    assert tpl.render("{year|default:Undated}/{year}-{month}", ctx) == "Undated/pic.jpg"
    assert tpl.render("{unknown}/{album}/keep", ctx) == "keep/pic.jpg"
    assert tpl.render("{month|pad2}", dict(CTX, month="3")) == "03/pic.jpg"


def test_render_sanitises_and_confines():
    def clean(s):
        return s.replace("?", "_")
    assert tpl.render("/../{album}?x/..", dict(CTX, album="a/b"), clean) == "a-b_x/pic.jpg"
    assert tpl.render("", CTX) == "pic.jpg"
    assert not tpl.render("{year}", CTX).startswith("/")


def test_force_owner_tree():
    assert tpl.force_owner_tree("2021/a.jpg", "users/ann/x/a.jpg") == "users/ann/2021/a.jpg"
    assert tpl.force_owner_tree("users/ann/2021/a.jpg", "users/ann/x/a.jpg") == "users/ann/2021/a.jpg"
    assert tpl.force_owner_tree("2021/a.jpg", "public/a.jpg") == "2021/a.jpg"
    assert tpl.with_suffix("a/b.jpg", 2) == "a/b (2).jpg"


# -- preview / run on real files --------------------------------------------------------
def test_preview_uses_dates_and_reports_in_place(client, host, upload, templates):
    templates(template_image="{year|default:Undated}/{year}-{month}", skip_folders="imports",
              date_fallback_mtime=True)
    a = upload("ra.png", seed=31, folder=FOLDER)
    b = upload("rb.png", seed=32, folder=FOLDER)
    _set_date(host, a, "2021-03-04T10:00:00")
    _set_date(host, b, None)
    j = _post(client, "/api/reorganize/preview", {"filenames": [a, b]})
    by = {i["from"]: i for i in j["items"]}
    assert by[a]["to"] == "2021/2021-03/ra.jxl" and by[a]["changed"]
    assert by[b]["changed"] and by[b]["to"].startswith("20")          # mtime fallback
    templates(date_fallback_mtime=False)
    j = _post(client, "/api/reorganize/preview", {"filenames": [b]})
    assert j["items"][0]["to"] == "Undated/rb.jxl"
    # a file already at its target is reported, not moved
    templates(template_image=FOLDER)
    j = _post(client, "/api/reorganize/preview", {"folder": FOLDER})
    assert all(not i["changed"] and i["reason"] == "already in place" for i in j["items"])


def test_run_moves_file_with_sidecar_and_rows(client, host, upload, templates):
    templates(template_image="{year}/{album|default:NoAlbum}", date_fallback_mtime=True)
    fn = upload("rc.png", seed=33, folder=FOLDER)
    write_meta(client, fn, tags=["keeptag"])
    _set_date(host, fn, "2020-05-06T10:00:00")
    client.post("/api/albums/create", json={"name": "ReorgAlbum"})
    assert _post(client, "/api/albums/add", {"album": "ReorgAlbum", "files": [fn]})["added"] == 1
    j = _post(client, "/api/reorganize/run", {"filenames": [fn], "dry_run": True, "sync": True})
    assert j["dry_run"] and os.path.exists(_media(fn))                   # a dry run moves nothing
    j = _post(client, "/api/reorganize/run", {"filenames": [fn], "dry_run": False, "sync": True})
    st = j["status"]
    assert st["moved"] == 1 and not st["errors"], st
    new = [r[0] for r in host.db().execute(
        "SELECT rel_to FROM reorganize_log WHERE rel_from=?", (fn,))][0]
    try:
        assert new == "2020/ReorgAlbum/rc.jxl"
        assert os.path.exists(_media(new)) and not os.path.exists(_media(fn))
        assert os.path.exists(_media(os.path.splitext(new)[0] + ".xmp"))
        assert not os.path.exists(_media(FOLDER))                           # emptied folder removed
        lst = client.get("/api/list", query_string={"folder": os.path.dirname(new)}).get_json()
        assert any(f["filename"] == new for f in lst["files"])
        assert read_meta(client, new)["tags"] == ["keeptag"]
        members = [r[0] for r in host.db().execute(
            "SELECT rel_path FROM album_members WHERE album='ReorgAlbum'")]
        assert members == [new]                                             # album membership followed
        assert host.db().execute("SELECT 1 FROM files WHERE rel_path=?", (fn,)).fetchone() is None
        s = client.get("/api/reorganize/status").get_json()
        assert s["success"] and not s["running"] and s["last_run"]["moved"] == 1
        # undo brings it back
        j = _post(client, "/api/reorganize/undo", {})
        assert j["restored"] == 1
        assert os.path.exists(_media(fn)) and not os.path.exists(_media(new))
        assert read_meta(client, fn)["tags"] == ["keeptag"]
    finally:
        _cleanup(client, [new, fn])
        client.post("/api/albums/delete", json={"name": "ReorgAlbum"})


def test_collisions_get_suffixes(client, host, upload, templates):
    templates(template_image="reorg_dst/{name|default:x}{ext}")
    a = upload("same.png", seed=34, folder=FOLDER + "/one")
    b = upload("same.png", seed=35, folder=FOLDER + "/two")
    j = _post(client, "/api/reorganize/preview", {"filenames": [a, b]})
    tos = [i["to"] for i in j["items"]]
    assert tos == ["reorg_dst/same.jxl", "reorg_dst/same (2).jxl"]
    assert j["items"][1]["reason"] == "collision"
    j = _post(client, "/api/reorganize/run", {"filenames": [a, b], "dry_run": False, "sync": True})
    assert j["status"]["moved"] == 2, j
    try:
        assert os.path.exists(_media("reorg_dst/same.jxl")) and os.path.exists(_media("reorg_dst/same (2).jxl"))
        assert not os.path.exists(_media(FOLDER, "one")) and not os.path.exists(_media(FOLDER, "two"))
        j = _post(client, "/api/reorganize/undo", {})
        assert j["restored"] == 2 and os.path.exists(_media(a)) and os.path.exists(_media(b))
    finally:
        _cleanup(client, ["reorg_dst/same.jxl", "reorg_dst/same (2).jxl", a, b])


def test_skip_folders_and_owner_tree(client, host, upload, templates):
    templates(template_image="flat", skip_folders="imports, reorg_skip", keep_owner_tree=True)
    s = upload("sk.png", seed=36, folder="reorg_skip/sub")
    d = upload("dt.png", seed=37, folder=".reorg_dot")
    o = upload("ow.png", seed=38, folder="users/reorg_user/photos")
    j = _post(client, "/api/reorganize/preview", {"filenames": [s, d, o]})
    by = {i["from"]: i for i in j["items"]}
    assert by[s]["reason"] == "skipped folder" and not by[s]["changed"]
    assert by[d]["reason"] == "dot folder" and not by[d]["changed"]
    assert by[o]["to"] == "users/reorg_user/flat/ow.jxl" and by[o]["changed"]
    j = _post(client, "/api/reorganize/run", {"filenames": [s, d], "dry_run": False, "sync": True})
    assert j["status"]["moved"] == 0 and j["status"]["skipped"] == 2
    assert os.path.exists(_media(s)) and os.path.exists(_media(d))


def test_scope_and_nothing_to_undo(client, host, upload, templates):
    templates(template_image="{year}")
    fn = upload("sc.png", seed=39, folder=FOLDER + "/deep")
    j = _post(client, "/api/reorganize/preview", {"folder": FOLDER})
    assert any(i["from"] == fn for i in j["items"])                       # subtree scope
    assert client.post("/api/reorganize/run", json={"filenames": ["nope/missing.jxl"], "sync": True}
                       ).get_json()["status"]["skipped"] == 1
    host.db().execute("DELETE FROM reorganize_log"); host.db().commit()
    r = client.post("/api/reorganize/undo", json={})
    assert r.status_code == 404
    t = client.get("/api/reorganize/tokens").get_json()
    assert t["success"] and "year" in t["tokens"] and t["templates"]["image"] == "{year}"
