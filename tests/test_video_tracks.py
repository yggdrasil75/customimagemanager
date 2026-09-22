"""video_tracks.py: sidecar save/load + keyframe interpolation."""
import os
import video_tracks as vt


def _doc():
    return {"tracks": [
        {"id": "t1", "label": "jill", "class_name": "person",
         "keyframes": [{"t": 2, "cx": .6, "cy": .6, "w": .2, "h": .2},
                       {"t": 0, "cx": .2, "cy": .2, "w": .2, "h": .2},
                       {"t": 4, "cx": .8, "cy": .8, "w": .2, "h": .2, "outside": True},
                       {"t": 6, "cx": .9, "cy": .9, "w": .2, "h": .2}]},
        {"id": "empty", "keyframes": []},
        {"keyframes": [{"t": 0, "cx": 1.5, "cy": -1, "w": "bad", "h": .1}]},
    ]}


def test_save_load_cleans(tmp_path):
    v = str(tmp_path / "clip.mp4")
    out = vt.save(v, _doc())
    assert os.path.exists(vt.sidecar_path(v))
    assert [t["id"] for t in out["tracks"]] == ["t1"]            # empty + all-bad dropped
    assert [k["t"] for k in out["tracks"][0]["keyframes"]] == [0, 2, 4, 6]   # sorted
    assert vt.load(v) == out
    assert vt.labels(vt.load(v)) == ["jill"]
    vt.save(v, {"tracks": []})
    assert not os.path.exists(vt.sidecar_path(v))                # emptied → sidecar removed
    assert vt.load(v) == {"version": 1, "tracks": []}


def test_clamp_keeps_track():
    t = vt._clean_track({"keyframes": [{"t": 0, "cx": 1.5, "cy": -1, "w": .5, "h": .1}]})
    assert t["keyframes"][0]["cx"] == 1.0 and t["keyframes"][0]["cy"] == 0.0
    assert t["class_name"] == "object" and t["id"].startswith("t_")


def test_box_at_interpolation_and_gaps(tmp_path):
    tr = vt.save(str(tmp_path / "c.mp4"), _doc())["tracks"][0]
    assert vt.box_at(tr, -1) is None and vt.box_at(tr, 7) is None
    assert vt.box_at(tr, 0)["cx"] == .2
    b = vt.box_at(tr, 1)
    assert abs(b["cx"] - .4) < 1e-9                              # halfway 0→2
    assert vt.box_at(tr, 5) is None                              # inside the 'outside' gap
    assert vt.box_at(tr, 6)["cx"] == .9
    assert len(vt.boxes_at({"tracks": [tr]}, 1)) == 1
    assert vt.boxes_at({"tracks": [tr]}, 5) == []