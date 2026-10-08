"""! @file
@brief slideshow: playlist ordering and the /api/slideshow routes.
    ./run_tests.sh modules/slideshow
"""
import pytest

from modules.slideshow import module as ssm


def _kind(rel):
    return "video" if rel.endswith(".mp4") else ("book" if rel.endswith(".epub") else "image")


def test_build_playlist_keeps_order_and_drops_books():
    rows = ["a.jpg", "b.epub", "c.mp4", "d.jpg"]
    items = ssm.build_playlist(rows, _kind)
    assert [i["filename"] for i in items] == ["a.jpg", "c.mp4", "d.jpg"]
    assert [i["kind"] for i in items] == ["image", "video", "image"]


def test_build_playlist_rotates_to_start():
    rows = ["a.jpg", "b.jpg", "c.jpg", "d.jpg"]
    items = ssm.build_playlist(rows, _kind, start="c.jpg")
    assert [i["filename"] for i in items] == ["c.jpg", "d.jpg", "a.jpg", "b.jpg"]
    # an unknown start leaves the order alone
    items = ssm.build_playlist(rows, _kind, start="zz.jpg")
    assert [i["filename"] for i in items] == rows


def test_build_playlist_shuffle_keeps_start_first():
    rows = ["%02d.jpg" % n for n in range(30)]
    items = ssm.build_playlist(rows, _kind, start="07.jpg", shuffle=True, seed=1)
    assert items[0]["filename"] == "07.jpg"
    assert sorted(i["filename"] for i in items) == rows
    assert [i["filename"] for i in items] != rows


def test_order_sql_follows_sort_tokens():
    assert ssm.order_sql([]) == "rel_path"
    assert ssm.order_sql([("tag", "x", False), ("sort", "mtime", True)]) == "mtime DESC, rel_path"


def test_validators():
    assert ssm._clamp_interval("2.5") == 2.5
    assert ssm._clamp_interval(0) == 1.0
    assert ssm._clamp_interval(99999) == 3600.0
    with pytest.raises(ValueError):
        ssm._clamp_interval("abc")
    check = ssm._one_of(ssm.TRANSITIONS)
    assert check("fade") == "fade"
    with pytest.raises(ValueError):
        check("wipe")


def test_routes(client, host):
    d = client.get("/api/slideshow/prefs").get_json()
    assert d["success"] and d["prefs"]["interval"] == 5 and d["prefs"]["transition"] == "fade"

    d = client.get("/api/slideshow/list?q=").get_json()
    assert d["success"] and isinstance(d["files"], list) and "prefs" in d

    d = client.get("/api/slideshow/list?q=sem:cat").get_json()
    assert not d["success"]

    d = client.post("/api/slideshow/list", json={"files": ["x/a.jpg", "x/b.mp4", "x/c.epub"],
                                                 "start": "x/b.mp4"}).get_json()
    assert d["success"]
    assert [f["filename"] for f in d["files"]] == ["x/b.mp4", "x/a.jpg"]
    assert "slideshow_interval" in host.user_settings
