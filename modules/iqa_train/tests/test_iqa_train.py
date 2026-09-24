"""iqa_train: dataset/label parsing, size table, and a build through a fake
personal_iqa service (no torch needed for the parsing parts).
    python -m pytest -q tests/test_iqa_train.py
"""
import logging
import os
import threading
import types

import numpy as np
import pytest

from modules.iqa_train import build as bd


def _imgs(d, names):
    cv2 = pytest.importorskip("cv2")
    for n in names:
        cv2.imwrite(os.path.join(d, n), np.zeros((16, 16, 3), np.uint8))


def test_parse_dataset_lines(tmp_path):
    (tmp_path / "labels.csv").write_text("a.jpg,5\n")
    lines = f"{tmp_path} {tmp_path}/x.csv\n{tmp_path}\n# c\n"
    ds = bd.parse_dataset_lines(lines)
    assert ds[0] == (str(tmp_path), str(tmp_path / "x.csv"))
    assert ds[1] == (str(tmp_path), str(tmp_path / "labels.csv"))   # auto-found


def test_read_labels_ava_and_csv(tmp_path):
    _imgs(str(tmp_path), ["1.jpg", "2.jpg", "3.png"])
    ava = tmp_path / "AVA.txt"
    ava.write_text("0 1 0 0 0 0 0 0 0 0 0 10 1 2 3\n1 2 10 0 0 0 0 0 0 0 0 0 1 2 3\n2 9 1 1 1 1 1 1 1 1 1 1 1 2 3\n")
    rows = dict(bd.read_labels(str(tmp_path), str(ava)))
    assert rows[str(tmp_path / "1.jpg")] == pytest.approx(1.0)
    assert rows[str(tmp_path / "2.jpg")] == pytest.approx(0.1)
    assert len(rows) == 2                                             # image 9 missing on disk
    csvf = tmp_path / "l.csv"
    csvf.write_text("name,score\n1.jpg,7.5\n3,2.5\n")
    rows = dict(bd.read_labels(str(tmp_path), str(csvf)))
    assert rows[str(tmp_path / "1.jpg")] == pytest.approx(0.75)
    assert rows[str(tmp_path / "3.png")] == pytest.approx(0.25)
    assert bd.read_labels(str(tmp_path), None) == []


def test_iqa_size_table_and_params():
    try:
        from modules.personal_iqa import net
    except Exception as e:                    # torch missing or broken
        pytest.skip(f"torch: {e}")
    tbl = net.parse_sizes("tiny 40 1\nwide 264,3")
    assert tbl == {"tiny": {"d": 40, "depth": 1}, "wide": {"d": 264, "depth": 3}}
    assert net.parse_sizes("") == net.SIZES
    dims = {"embed": 16, "tile": 16, "face": 215, "pose17": 34, "pose133": 266, "iqa": 1}
    m = net.Scorer(dims, 64, 2)
    assert sum(p.numel() for p in m.parameters()) == net.count_params(dims, 64, 2)


def test_missing_parts_is_about_models_having_run():
    from modules.personal_iqa import module as piqa
    req = ["embed", "iqa", "face", "pose", "tags"]
    landscape = {"embed": [[1]], "iqa": [[0.5]], "face": [], "pose17": [], "tags": [0],
                 "_processed": {"face": True, "pose": True, "tags": True}}
    assert piqa.missing_parts(landscape, req) == []                    # nothing found, but processed
    unscanned = {"embed": [[1]], "iqa": [], "face": [[0]], "tags": [3], "_processed": {"face": False, "pose": False}}
    assert piqa.missing_parts(unscanned, req) == ["iqa", "face", "pose"]


def test_pose_tokens_default_is_coco_only():
    from modules.pose import skeleton as sk
    kps = [{"x": 0.5 + 0.01 * i, "y": 0.1 + 0.05 * i, "v": 1.0} for i in range(17)]
    t = sk.tokens(kps)
    assert t["kind"] == "pose17" and len(t["norm"]) == 34 and len(t["raw"]) == 51 and len(t["bones"]) == 18
    big = sk.tokens([dict(k, x=k["x"] * 3, y=k["y"] * 3) for k in kps])
    assert big["bones"] == pytest.approx(t["bones"], rel=1e-4)             # proportions are scale-free
    assert big["norm"] == pytest.approx(t["norm"], rel=1e-4) and big["raw"] != t["raw"]
    assert sk.tokens([{"x": 0, "y": 0, "v": 0}] * 133)["kind"] == "pose133"
    assert sk.tokens([{"x": 0, "y": 0, "v": 0}] * 33) is None               # not COCO: the provider's job


def test_mediapipe_provider_owns_its_conversion():
    from modules.mediapipe_pose import module as mp
    from modules.pose import skeleton as sk
    blaze = [{"x": i / 33, "y": 0.5, "v": 1.0} for i in range(33)]
    assert len(mp.to_coco(blaze)) == 17 and mp.to_coco(blaze)[5] is blaze[11]      # left shoulder
    hol = [{"x": i / 543, "y": 0.5, "v": 1.0} for i in range(543)]
    d = mp.to_coco(hol)
    assert len(d) == 133 and d[91] is hol[501] and d[132] is hol[542] and d[19] is hol[29]
    assert len(set(mp.MESH468_TO_68)) == 68 and max(mp.MESH468_TO_68) < 468
    assert mp.to_coco(d) is d                                                     # already converted
    assert sk.tokens(mp.to_coco(hol))["kind"] == "pose133"


def _fake_service(tmp):
    """Stand-in for personal_iqa: constant features, records fits, writes files."""
    saved = []
    def features(db, key, mtime):
        # every third image has no face -> must be skipped and counted
        nface = [] if key.endswith(("0.jpg", "3.jpg", "6.jpg", "9.jpg")) else [[0.0] * 215]
        return {"embed": [[0.5] * 4], "tile": [], "face": nface, "pose17": [[0.0] * 34], "pose133": [],
                "iqa": [[0.4]], "tags": [0] * 32, "_base": 0.4, "_missing": [] if nface else ["face"]}
    def fit(train, val, d, depth, epochs, batch, lr, say, stop):
        say(epochs, 0.01)
        m = types.SimpleNamespace(parameters=lambda: [np.zeros(d * depth)])
        m.parameters = lambda: [types.SimpleNamespace(numel=lambda: d * depth)]
        return m, {"n_val": len(val), "val_spearman": 0.5, "base_spearman": 0.3, "val_mse": 0.02, "d": d, "depth": depth}
    def save(m, metrics, path=None):
        p = path or os.path.join(tmp, "scorer.pt"); open(p, "w").write("x"); saved.append(p)
    return {"features": features, "fit": fit, "save": save, "ckpt_path": os.path.join(tmp, "scorer.pt"),
            "ckpt_dir": tmp, "count_params": lambda dims, d, depth: d * depth, "Scorer": None, "batch": None,
            "sizes": lambda: {"a": {"d": 8, "depth": 1}, "b": {"d": 16, "depth": 1}},
            "required": lambda: ["embed", "iqa", "face", "pose"], "token_dims": {"face": 215, "iqa": 1},
            "detectors": lambda: {"embed": True, "iqa": True, "face": True, "pose": True}}, saved


def test_build_with_fake_service(tmp_path):
    _imgs(str(tmp_path), [f"{i}.jpg" for i in range(30)])
    (tmp_path / "labels.csv").write_text("".join(f"{i}.jpg,{(i % 10) + 1}\n" for i in range(30)))
    svc, saved = _fake_service(str(tmp_path))
    host = types.SimpleNamespace(config={}, logger=logging.getLogger("t"),
                                 get_service=lambda n: svc if n == "personal_iqa" else None,
                                 db=lambda: types.SimpleNamespace(execute=lambda *a: types.SimpleNamespace(fetchone=lambda: None, fetchall=lambda: []), commit=lambda: None),
                                 core=types.SimpleNamespace(db_close=lambda: None))
    got = []
    s = bd.build(host, [(str(tmp_path), str(tmp_path / "labels.csv"))], sizes=svc["sizes"](), active="a",
                 epochs=2, on_installed=lambda z: got.append(z) or True)
    assert s["ok"], s.get("error")
    assert s["datasets"][str(tmp_path)] == 30 and s["skipped"] == {"face": 12}
    assert set(s["sizes"]) == {"a", "b"} and s["sizes"]["a"]["val_spearman"] == 0.5
    assert got == ["a"] and os.path.join(str(tmp_path), "scorer.pt") in saved
    assert os.path.exists(tmp_path / "scorer_b.pt")
    assert bd.build(host, [], sizes=svc["sizes"]())["error"]
    svc["detectors"] = lambda: {"embed": True, "iqa": True, "face": False, "pose": True}
    assert "face" in bd.build(host, [], sizes=svc["sizes"]())["error"]     # refuses without a face provider