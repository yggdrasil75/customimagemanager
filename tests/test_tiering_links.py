"""! @file
@brief Tier objects that several library links share (a file copied with its symlink
kept, cp -a / rsync -l): moving or renaming the object takes every link along,
deleting one copy keeps the object for the others, and links left dangling by an
older version are relinked to the object where it is now."""
import os

import pytest

import tiering


@pytest.fixture
def lib(tmp_path):
    media, tier0, tier1 = tmp_path / "media", tmp_path / "t0", tmp_path / "t1"
    media.mkdir()
    ids = {}
    saved = {k: tiering._state.get(k) for k in ("cfg", "media_dir", "read_document_id",
                                                 "ensure_document_id", "links")}
    tiering._state.update(
        cfg={**tiering.DEFAULT_CFG, "enabled": True,
             "tiers": [{"name": "a", "path": str(tier0), "ratio": 50, "speed_mbps": 500},
                       {"name": "b", "path": str(tier1), "ratio": 50, "speed_mbps": 500}]},
        media_dir=str(media), links=None,
        read_document_id=lambda p: ids.get(os.path.splitext(os.path.basename(p))[0]),
        ensure_document_id=lambda p, i=None: ids.setdefault(os.path.splitext(os.path.basename(p))[0],
                                                            i or "id" + os.path.basename(p)))
    yield media, tier0, tier1, ids
    tiering._state.update(saved)


def _obj(tier, name, data=b"x" * 10):
    root = os.path.join(tiering._object_root(str(tier)), name[:2])
    os.makedirs(root, exist_ok=True)
    p = os.path.join(root, name)
    with open(p, "wb") as f:
        f.write(data)
    return p


def _link(media, rel, target):
    p = os.path.join(str(media), rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    os.symlink(target, p)
    return p


def test_a_move_takes_every_copy_of_the_link_along(lib):
    media, t0, _t1, ids = lib
    obj = _obj(t0, "abcd.jxl")
    a = _link(media, "photos/a.jxl", obj)
    b = _link(media, "photos/temp/a.jxl", obj)          # copied with the symlink kept
    ids["a"] = "abcd"
    tiering._links_by_object(str(media))
    assert tiering._execute_move({"rel": "photos/a.jxl", "to": 1, "size": 10}, tiering._state["cfg"], mbps=0)
    assert os.path.exists(a) and os.path.exists(b)
    assert os.path.realpath(a) == os.path.realpath(b) != obj and not os.path.exists(obj)


def test_adopt_renames_the_object_for_every_link(lib):
    media, t0, _t1, ids = lib
    obj = _obj(t0, "0123uuid.jxl")
    a = _link(media, "p/a.jxl", obj)
    b = _link(media, "p/temp/a.jxl", obj)
    ids["a"] = "feedid"                                   # the sidecar says another id
    assert tiering.adopt_ids() == 1
    assert os.path.exists(a) and os.path.exists(b)
    assert os.path.basename(os.path.realpath(b)) == "feedid.jxl"


def test_deleting_one_copy_keeps_the_object_for_the_other(lib):
    media, t0, _t1, _ids = lib
    obj = _obj(t0, "beef.jxl")
    a = _link(media, "x/a.jxl", obj)
    b = _link(media, "x/copy/a.jxl", obj)
    tiering._links_by_object(str(media))
    tiering.safe_remove(a)
    assert not os.path.lexists(a) and os.path.exists(b)
    tiering.safe_remove(b)                                # the last one takes the object
    assert not os.path.exists(obj)


def test_dangling_links_are_relinked_to_where_the_object_is(lib):
    media, t0, t1, ids = lib
    moved = _obj(t1, "cafe.jxl")                          # on the other tier now
    renamed = _obj(t0, "f00d.jxl")                        # under its DocumentID now
    a = _link(media, "d/a.jxl", os.path.join(tiering._object_root(str(t0)), "ca", "cafe.jxl"))
    b = _link(media, "d/b.jxl", os.path.join(tiering._object_root(str(t0)), "00", "00uuid.jxl"))
    ids["b"] = "f00d"
    gone = _link(media, "d/c.jxl", os.path.join(tiering._object_root(str(t0)), "zz", "zzzz.jxl"))
    fixed, broken = tiering.repair_dangling()
    assert sorted(fixed) == ["d/a.jxl", "d/b.jxl"]
    assert os.path.realpath(a) == moved and os.path.realpath(b) == renamed
    assert [r for r, _ in broken] == ["d/c.jxl"] and os.path.lexists(gone)