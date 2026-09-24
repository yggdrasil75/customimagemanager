"""training_validate: duplicate boxes (yours and the model's) and scoring."""
from modules.trainer import training_validate as tv


def b(cls, cx, cy, w, h, **k):
    return {"class_name": cls, "cx": cx, "cy": cy, "w": w, "h": h, **k}


def test_dups_split_and_scored():
    gt = [b("A", .5, .5, .2, .2), b("A", .505, .5, .24, .24),   # lazy re-draw, slightly bigger
          b("B", .2, .2, .1, .1)]
    pred = [b("A", .5, .5, .2, .2), b("A", .51, .5, .26, .26),  # model boxed A twice
            b("B", .2, .2, .1, .1), b("A", .8, .8, .1, .1)]    # plus one on background
    d = tv.diff_image(gt, pred)
    c = d["counts"]
    assert c["dup_gt"] == 1 and c["dup_pred"] == 1 and c["added"] == 1
    assert c["correct"] == 2 and c["dropped"] == 0
    s = tv.aggregate([d])
    assert s["n_pred"] == 4 and s["precision"] == 0.5 and s["recall"] == 1.0


def test_split_dups_keeps_first_and_ignores_other_class():
    kept, dups = tv.split_dups([b("A", .5, .5, .2, .2), b("B", .5, .5, .2, .2),
                                b("A", .5, .5, .18, .18)])
    assert [k["class_name"] for k in kept] == ["A", "B"] and len(dups) == 1


def test_debug_regions_roundtrip_and_stay_out_of_labels(client, upload):
    import os
    from cimtest import write_meta, read_meta, media_path
    fn = upload(seed=311)
    real = b("logo", .3, .3, .1, .1, confirmed=True)
    dbg = b("logo", .7, .7, .1, .1, confirmed=True, debug=True,
            region_description="debug; set=Set 1; model=set_Set_1_train_2; verdict=added; conf=0.410")
    write_meta(client, fn, regions=[real, dbg])
    regs = read_meta(client, fn)["regions"]
    d = [r for r in regs if r.get("debug")]
    assert len(regs) == 2 and len(d) == 1
    assert "model=set_Set_1_train_2" in d[0]["region_description"]
    with open(os.path.splitext(media_path(fn))[0] + ".txt") as f:
        assert len(f.read().strip().splitlines()) == 1     # only the real box is a label


def test_validate_debug_runs_and_snap(app, client, upload, monkeypatch, ungated):
    """Validate a numbered run with debug storage, re-validate (replaces, not
    stacks), then snap: one real box on the model's geometry, dup gone."""
    import os
    import numpy as np
    from cimtest import write_meta, read_meta
    from modules.trainer import trainer_core as tc
    fn = upload(seed=77)
    j = client.post("/api/trainer/select", json={"strategy": "recent", "n": 1}).get_json()
    assert j["success"], j
    s = j["set"]; wp = j["files"][0]["rel_path"]
    assert wp.startswith(".training_sets")
    gt = [{"class_name":"logo","cx":.5,"cy":.5,"w":.2,"h":.2,"confirmed":True},
          {"class_name":"logo","cx":.505,"cy":.5,"w":.24,"h":.24,"confirmed":True}]
    write_meta(client, wp, regions=gt)
    rd = os.path.join(os.path.abspath(tc.MODELS_DIR), "runs", "detect", tc._next_run_name(s))
    try:
        os.makedirs(os.path.join(rd, "weights")); open(os.path.join(rd, "weights", "best.pt"), "w").close()
        monkeypatch.setattr(tc, "_detect_obb_or_box", lambda *a, **k: [
            {"class_name":"logo","cx":.5,"cy":.5,"w":.22,"h":.22,"conf":.9},
            {"class_name":"logo","cx":.8,"cy":.8,"w":.1,"h":.1,"conf":.3}])
        monkeypatch.setattr(tc, "read_jxl", lambda p: np.zeros((32, 48, 3), np.uint8))
        runs = client.get("/api/trainer/runs?set=" + s).get_json()["runs"]
        r = client.post("/api/trainer/validate", json={"set": s, "run": runs[-1]["run"], "store_debug": True}).get_json()
        assert r["success"], r
        c = r["summary"]["counts"]
        assert c["dup_gt"] == 1 and c["added"] == 1 and c["tightened"] + c["correct"] == 1
        regs = read_meta(client, wp)["regions"]
        dbg = [x for x in regs if x.get("debug")]
        assert len(regs) == 4 and len(dbg) == 2, regs
        assert all(f"model={runs[-1]['run']}" in x["region_description"] and "set=" + s in x["region_description"] for x in dbg)
        assert os.path.exists(os.path.join(rd, "validation.json"))
        # second validation replaces (not stacks) this set's debug boxes
        client.post("/api/trainer/validate", json={"set": s, "store_debug": True})
        assert len([x for x in read_meta(client, wp)["regions"] if x.get("debug")]) == 2
        # snap: keeps one GT box on model geometry, debug untouched
        im = r["images"][0]
        snap = [dict(b["gt"], cx=b["pred"]["cx"], cy=b["pred"]["cy"], w=b["pred"]["w"], h=b["pred"]["h"])
                for b in im["boxes"] if b["gt"] and b["pred"]]
        client.post("/api/trainer/apply_prediction", json={"filename": wp, "regions": snap, "classes": []})
        regs = read_meta(client, wp)["regions"]
        real = [x for x in regs if not x.get("debug")]
        assert len(real) == 1 and abs(real[0]["w"] - .22) < 1e-4 and len(regs) == 3
    finally:
        import shutil
        shutil.rmtree(rd, ignore_errors=True)