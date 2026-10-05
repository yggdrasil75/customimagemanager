"""iqa_train.pack -> train_pack: a feature pack is written from a labelled folder
(fake providers), trained standalone with ablation, and a rerun is all cache hits.
    python -m pytest -q modules/iqa_train/tests/test_pack.py
"""
import pytest

pytest.importorskip("torch")


def _fake_host(d, monkeypatch):
    """Minimal host with every capability the personal scorer asks for; a stripped
    copy of the one in modules/personal_iqa/tests/test_iqa.py."""
    import sqlite3, types, logging, os, numpy as np, cv2
    import model_registry
    from modules.model_broker import NoProviderError
    from modules.personal_iqa import module as piqa
    monkeypatch.setattr(model_registry, "model_dir", lambda *a: d + "/models")
    caps = {
        "embed": lambda img: np.ones(16, np.float32) * img.mean() / 255,
        "detect": lambda img: [{"class_name": "boat", "cx": .4, "cy": .5, "w": .3, "h": .3, "conf": .9}],
        "depth": lambda img: np.linspace(0, 1, img.shape[0])[:, None].repeat(img.shape[1], 1),
        "segment": lambda img: [{"class_name": "mountain", "mask": [(0, 1), (1, 1), (.5, .3)]}],
        "classify": lambda img: [{"class_name": "photo", "conf": .9}],
        "embed.text": lambda t: np.full(8, len(t) / 10, np.float32),
        "iqa": lambda img: {"quality": .6}, "detect.faces": lambda img: [], "pose": lambda img: [],
    }
    def request_model(cap, provider=None):
        if cap in caps:
            return caps[cap]
        raise NoProviderError(cap)
    db = sqlite3.connect(":memory:", check_same_thread=False); db.row_factory = sqlite3.Row
    db.executescript("""CREATE TABLE files(rel_path TEXT PRIMARY KEY, mtime REAL, tags TEXT, face_done INT, autotag_done INT);
    CREATE TABLE ratings(rel_path TEXT PRIMARY KEY, user_stars INT);
    CREATE TABLE face_regions(rel_path TEXT, shape BLOB, cx REAL, cy REAL, w REAL, h REAL, not_face INT);""")
    core = types.SimpleNamespace(object_grouping=types.SimpleNamespace(downscale_to_cap=lambda x, **k: x),
                                 read_image=cv2.imread, to_bgr=lambda x: x, db_close=lambda: None,
                                 read_metadata=lambda fp: {"pose": {"people": []}}, embed_faces=lambda *a, **k: ([], [], []))
    class Broker:
        def selected_id(self, c, role=None): return c if c in caps else None
        def providers_for(self, c): return []
    svc, startup = {}, []
    host = types.SimpleNamespace(core=core, config={"personal_iqa_base": "brisque", "personal_iqa_encoder": ""},
        db=lambda: db, broker=Broker(), request_model=request_model, logger=logging.getLogger("t"),
        media_dir=f"{d}/media", safe_path=lambda m, r: os.path.join(m, r), save_config=lambda: None,
        get_service=lambda n: svc if n == "personal_iqa" and svc else None, provide_service=lambda n, s: svc.update(s),
        provide_model=lambda *a, **k: None, add_asset=lambda *a: None, add_settings_tab=lambda *a, **k: None,
        add_table=lambda ddl: db.executescript(ddl), add_settings_field=lambda **k: None,
        add_config_key=lambda k, default=None, **kw: host.config.setdefault(k, default),
        on_startup=lambda f: startup.append(f), add_route=lambda *a, **k: None, on=lambda *a: None)
    piqa.register(host); [f() for f in startup]
    return host


def test_pack_then_train(tmp_path, monkeypatch):
    import os, numpy as np, cv2, torch
    from modules.iqa_train import build as bd, train_pack as tp
    d = str(tmp_path)
    host = _fake_host(d, monkeypatch)
    os.makedirs(f"{d}/ds")
    with open(f"{d}/ds/labels.csv", "w") as f:
        for i in range(12):
            cv2.imwrite(f"{d}/ds/{i}.jpg", np.random.randint(0, 255, (64, 80, 3), np.uint8))
            f.write(f"{i}.jpg,{i % 10 + 1}\n")
    ds = [(f"{d}/ds", f"{d}/ds/labels.csv")]
    res = bd.pack(host, ds, name="t", holdout=25)
    assert res["ok"] and res["n_train"] + res["n_val"] == 12 and res["n_val"] >= 1, res
    p = res["profile"]
    assert p["images_computed"] == 12 and set(p["ms_per_image"]) >= {"embed", "tiles", "objects", "depth", "regions"}
    rep = tp.main([res["path"], "--out", f"{d}/out", "--sizes", "nano 16 1", "--epochs", "2", "--batch", "4",
                   "--ablate", "nano", "--device", "cpu"])
    assert "nano" in rep["sizes"] and "tag_text" in rep["ablation"] and os.path.exists(f"{d}/out/report.json")
    ck = torch.load(f"{d}/out/scorer_nano.pt", weights_only=False)
    assert set(ck) == {"dims", "d", "depth", "metrics", "state"} and ck["dims"]["tag_text"] == 8
    res2 = bd.pack(host, ds, name="t2", holdout=25)          # second pass: everything served from the cache
    assert res2["ok"] and res2["profile"]["images_cached"] == 12 and res2["profile"]["images_computed"] == 0


def test_engagement_labels(tmp_path, monkeypatch):
    import os, json, numpy as np, cv2
    from modules.iqa_train import build as bd
    d = str(tmp_path)
    host = _fake_host(d, monkeypatch)
    os.makedirs(f"{d}/ds")
    for i in range(4):
        cv2.imwrite(f"{d}/ds/{i}.jpg", np.random.randint(0, 255, (32, 32, 3), np.uint8))
    with open(f"{d}/ds/labels.csv", "w") as f:        # engagement CSV, header-detected
        f.write("name,score_up,score_down,views,source\n0.jpg,50,1,900,e621\n1.jpg,2,8,900,e621\n"
                "2.jpg,9,,300,safebooru\n3.jpg,4,,,\n")           # 3: no down, no views -> dropped
    rows = dict(bd.read_labels(f"{d}/ds", f"{d}/ds/labels.csv"))
    assert set(os.path.basename(k) for k in rows) == {"0.jpg", "1.jpg", "2.jpg"}
    assert rows[f"{d}/ds/0.jpg"] > rows[f"{d}/ds/1.jpg"]
    db = host.db()                                     # library: tags carry the counts, rated files excluded
    os.makedirs(f"{d}/media", exist_ok=True)
    for i, tags in enumerate((["score_up: 30", "score_down: 2", "source: e6"], ["score_up: 1", "views: 5000", "source: e6"],
                              ["score_up: 7", "score_down: 0"])):
        cv2.imwrite(f"{d}/media/s{i}.jpg", np.random.randint(0, 255, (32, 32, 3), np.uint8))
        db.execute("INSERT INTO files VALUES(?, 1.0, ?, 1, 1)", (f"s{i}.jpg", json.dumps(tags)))
    db.execute("INSERT INTO ratings VALUES('s2.jpg', 4)")
    got = dict(bd.scored_from_tags(db))
    assert set(got) == {"s0.jpg", "s1.jpg"} and got["s0.jpg"] > got["s1.jpg"]
    res = bd.pack(host, [], use_scores=True, name="e", holdout=1)
    assert res["ok"] and res["datasets"]["library score_up/views tags"] == 2 and res["n_train"] + res["n_val"] == 2