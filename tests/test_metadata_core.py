"""manager.py core (no HTTP): path safety, region merge, XMP write/read,
indexing, search parsing. Uses the shared app fixture (temp cwd)."""
import os
import numpy as np
import pytest


def _box(**kw):
    b = {"class_name": "person", "cx": .5, "cy": .5, "w": .4, "h": .8,
         "confirmed": False, "region_tags": [], "region_description": ""}
    b.update(kw); return b


def test_get_safe_path(app):
    base = app.MEDIA_DIR
    assert app.get_safe_path(base, "a/b.jxl").endswith(os.path.join("a", "b.jxl"))
    assert app.get_safe_path(base, "/a.jxl") is not None           # leading slash stripped
    assert app.get_safe_path(base, "../x") is None
    assert app.get_safe_path(base, "a/../../x") is None


def test_regions_overlap():
    import manager as m
    a = _box()
    assert m._regions_overlap(a, _box(cx=.51))                       # centre match
    assert m._regions_overlap(a, _box(w=.5))                         # IoU high
    assert not m._regions_overlap(a, _box(cx=.1, cy=.1, w=.1, h=.1))


def test_merge_regions_precedence_and_backfill():
    import manager as m
    hi = [_box(class_name="object", uuid=None)]
    lo = [_box(class_name="girl", uuid="u9", region_type="", confirmed=True,
               region_description="d")]
    out = m._merge_regions(hi, lo)
    assert len(out) == 1
    r = out[0]
    assert r["class_name"] == "girl" and r["uuid"] == "u9"
    assert r["confirmed"] is True and r["region_description"] == "d"
    # non-overlapping second source is appended, same-source duplicates are not folded
    out = m._merge_regions([_box(), _box(cx=.52)], [_box(cx=.1, cy=.1, w=.1, h=.1)])
    assert len(out) == 3


def _make_jxl(app, name, seed=0):
    import imagecodecs
    rng = np.random.default_rng(seed)
    img = rng.integers(0, 255, (24, 32, 3), dtype=np.uint8)
    p = os.path.join(app.MEDIA_DIR, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as fh:
        fh.write(imagecodecs.jpegxl_encode(img))
    return p


def test_write_read_metadata_roundtrip(app):
    p = _make_jxl(app, "core_rt.jxl")
    try:
        regions = [_box(), _box(class_name="face", cx=.3, cy=.3, w=.1, h=.1,
                                region_name="jill", region_tags=["smile"])]
        assert app.write_metadata(p, ["cat", "?dog"], "a desc", regions)
        assert os.path.exists(os.path.splitext(p)[0] + ".xmp")
        m = app.read_metadata(p)
        assert m["tags"] == ["cat", "?dog"] and m["description"] == "a desc"
        assert len(m["regions"]) == 2
        person, face = sorted(m["regions"], key=lambda r: r["class_name"], reverse=True)
        assert person["region_type"] == "person" and person["region_name"] == ""
        assert not person["confirmed"]
        assert face["region_name"] == "jill" and face["region_type"] == "face"
        assert face["region_tags"] and all(isinstance(r["uuid"], str) for r in m["regions"])
        # uuids survive a rewrite
        ids = {r["uuid"] for r in m["regions"]}
        app.write_metadata(p, m["tags"], m["description"], m["regions"])
        assert {r["uuid"] for r in app.read_metadata(p)["regions"]} == ids
    finally:
        app._purge_file_everywhere("core_rt.jxl")
        for e in (".jxl", ".xmp"):
            try: os.remove(os.path.splitext(p)[0] + e)
            except OSError: pass


def test_write_flag_pose_albums_persist(app):
    p = _make_jxl(app, "core_flag.jxl", seed=1)
    try:
        pose = {"people": [{"keypoints": [{"x": .5, "y": .5, "v": .9}] * 3}]}
        app.write_metadata(p, [], "", [], flag={"delete": True, "reason": "blurry"},
                           pose=pose, albums=["Trip"])
        m = app.read_metadata(p)
        assert m["flag"] and m["flag"].get("delete") is True
        assert m["pose"] and len(m["pose"]["people"]) == 1
        assert m["albums"] == ["Trip"]
        # None keeps the existing flag/pose/albums; only tags change
        app.write_metadata(p, ["x"], "", [])
        m = app.read_metadata(p)
        assert m["tags"] == ["x"] and m["flag"]["delete"] is True and m["albums"] == ["Trip"]
        app.write_metadata(p, [], "", [], pose={"clear": True}, flag={"delete": False, "reason": ""})
        m = app.read_metadata(p)
        assert not m["pose"] and not (m["flag"] or {}).get("delete")
    finally:
        app._purge_file_everywhere("core_flag.jxl")
        for e in (".jxl", ".xmp"):
            try: os.remove(os.path.splitext(p)[0] + e)
            except OSError: pass


def test_index_file_and_query(app):
    p = _make_jxl(app, "idx/core_idx.jxl", seed=2)
    rel = "idx/core_idx.jxl"
    try:
        assert app._index_file(rel, force=True)
        row = app._get_file_row(rel)
        assert row and row["width"] == 32 and row["height"] == 24
        assert app._index_file(rel) is False                        # unchanged mtime → skip
        app.write_metadata(p, ["zebra"], "striped", [_box()])
        entries, total = app._query_files("zebra", 0, 50)
        assert total == 1 and entries[0]["filename"] == rel
        assert app._query_files("is:unconfirmed", 0, 50)[1] == 1
        assert app._query_files("is:tagged", 0, 50, folder="idx")[1] == 1
        assert app._query_files("nothinghere", 0, 50)[1] == 0
        assert app._query_files("", 0, 50, folder="nope")[1] == 0
        app._purge_file_everywhere(rel)
        assert app._get_file_row(rel) is None
    finally:
        for e in (".jxl", ".xmp"):
            try: os.remove(os.path.splitext(p)[0] + e)
            except OSError: pass


def test_region_desc_json_roundtrip(app):
    r = _box(class_name="girl", region_type="Full body", region_description="d",
             region_tags=["a", "?b"])
    raw = app._region_desc_to_json(r)
    desc, tags, cls = app._region_desc_from_json(raw)
    assert desc == "d" and cls == "girl"
    assert [t["tag"] for t in tags] == ["a", "?b"] and not tags[0]["generated"]
    # type==class → class not duplicated in the JSON
    assert app._region_desc_from_json(app._region_desc_to_json(_box(region_type="person")))[2] == ""
    assert app._region_desc_from_json("not json")[0] == "not json"
    assert app._region_desc_from_json("") == ("", [], "")