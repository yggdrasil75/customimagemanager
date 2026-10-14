"""! @file
@brief Region data that used to be dropped on write: detector confidence, oriented
boxes, segmentation polygons, a region's own skeleton and the needs_review /
is_animal flags survive the sidecar; the geometry helpers behind rotation."""
import math

import pytest

import common
from cimtest import box, media_path, read_meta


def _kps(n=17, x0=.4, y0=.2):
    return [{"x": round(x0 + (i % 5) * .03, 4), "y": round(y0 + i * .03, 4), "v": .9} for i in range(n)]


def test_extension_fields_round_trip(client, host, upload):
    fn = upload(seed=8101, name="ext.png")
    obb = [[.40, .35], [.60, .36], [.60, .65], [.40, .64]]
    poly = [[.4, .4], [.6, .4], [.55, .6], [.45, .6]]
    regions = [
        box(class_name="cat", cx=.5, cy=.5, w=.2, h=.3, score=.913, obb=obb, polygon=poly,
            needs_review=True, is_animal=True),
        box(class_name="person", cx=.3, cy=.5, w=.3, h=.8, conf=.77,
            pose={"kind": "body", "model": "rtmpose", "conf": .66, "keypoints": _kps()}),
        box(class_name="plain"),
    ]
    assert host.update_file(fn, set={"regions": regions})["success"]
    got = {r["class_name"]: r for r in host.core.read_metadata(media_path(fn))["regions"]}
    cat, person, plain = got["cat"], got["person"], got["plain"]
    assert cat["conf"] == pytest.approx(.913, abs=1e-4)
    flat = lambda pts: [c for p in pts for c in p]
    assert flat(cat["obb"]) == pytest.approx(flat(obb)) and flat(cat["polygon"]) == pytest.approx(flat(poly))
    assert cat["needs_review"] is True and cat["is_animal"] is True
    assert person["conf"] == pytest.approx(.77)
    p = person["pose"]
    assert p["kind"] == "body" and p["model"] == "rtmpose" and p["conf"] == pytest.approx(.66)
    assert [(k["x"], k["y"]) for k in p["keypoints"]] == pytest.approx([(k["x"], k["y"]) for k in _kps()])
    for k in ("conf", "obb", "polygon", "pose", "needs_review", "is_animal"):
        assert k not in plain
    # the API serves them too
    api = {r["class_name"]: r for r in read_meta(client, fn)["regions"]}
    assert api["person"]["pose"]["keypoints"] and api["cat"]["polygon"]


def test_oriented_box_from_angle_gets_corners_and_bounds(host, upload):
    fn = upload(seed=8102, name="obb.png")          # 48 x 32
    W, H = 48, 32
    host.update_file(fn, set={"regions": [box(class_name="ship", cx=.5, cy=.5, w=.5, h=.25,
                                              angle=math.pi / 2, conf=.8)]})
    r = host.core.read_metadata(media_path(fn))["regions"][0]
    # a 24 x 8 px box turned a quarter: 8 x 24 px around the centre
    assert len(r["obb"]) == 4 and "angle" not in r
    assert r["w"] == pytest.approx(8 / W, abs=1e-4) and r["h"] == pytest.approx(24 / H, abs=1e-4)
    assert abs(abs(common.obb_angle(r["obb"], W, H)) - math.pi / 2) < 1e-3


def test_obb_corners_square_and_bounds():
    c = common.obb_corners(.5, .5, .2, .2, 0.0, 100, 100)
    assert c == [[.4, .4], [.6, .4], [.6, .6], [.4, .6]]
    b = common.points_bounds(common.obb_corners(.5, .5, .2, .2, math.pi / 4, 100, 100))
    assert b["w"] == pytest.approx(.2 * math.sqrt(2), abs=1e-5)


def test_mirror_swaps_left_and_right_keypoints():
    kps = [{"x": .1 * (i % 9), "y": .05 * i, "v": 1} for i in range(17)]
    out = common.orient_keypoints(2, kps)                       # mirror left-right
    # left_shoulder (5) now holds the old right_shoulder (6), mirrored
    assert out[5]["x"] == pytest.approx(1 - kps[6]["x"]) and out[5]["y"] == pytest.approx(kps[6]["y"])
    assert out[0]["x"] == pytest.approx(1 - kps[0]["x"])        # the nose keeps its slot
    turned = common.orient_keypoints(6, kps)                    # quarter turn: no swap
    assert turned[5]["x"] == pytest.approx(1 - kps[5]["y"]) and turned[5]["y"] == pytest.approx(kps[5]["x"])
    for n in (17, 33, 133, 543):                                # each flip map is a permutation
        perm = common.KEYPOINT_FLIP[n]
        assert sorted(perm) == list(range(n)) and all(perm[perm[i]] == i for i in range(n))


def test_sidecar_blobs_survive_an_xmp_patch(host, upload):
    """! exiv2 writes the mm:* blobs back as attributes after any EXIF / XMP patch; they
    used to read as missing there, and the next full sidecar write dropped them."""
    fn = upload(seed=8103, name="blobs.png")
    kps = _kps()
    host.update_file(fn, set={"pose": {"kind": "body", "people": [{"keypoints": kps}]},
                              "analysis": {"summary": "kept"}, "flag": {"delete": True, "reason": "r"}})
    assert host.update_file(fn, xmp={"Xmp.dc.source": "http://example.com/x"})["success"]
    m = host.core.read_metadata(media_path(fn))
    assert m["pose"]["people"][0]["keypoints"][3]["y"] == pytest.approx(kps[3]["y"])
    assert (m.get("analysis") or {}).get("summary") == "kept"
    assert (m.get("flag") or {}).get("reason") == "r"
    # and a full rewrite afterwards keeps them
    host.update_file(fn, set={"regions": [box()]})
    m = host.core.read_metadata(media_path(fn))
    assert m["pose"]["people"] and (m.get("analysis") or {}).get("summary") == "kept"