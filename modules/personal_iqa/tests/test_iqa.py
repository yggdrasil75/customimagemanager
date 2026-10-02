"""personal_iqa: every token type is produced from fake providers, cached,
fitted, and an old checkpoint grows into the new token types.
    python -m pytest -q modules/personal_iqa/tests/test_features.py
"""
import pytest

pytest.importorskip("torch")


def test_features_fit_grow(tmp_path, monkeypatch):
    d = str(tmp_path)
    import model_registry
    monkeypatch.setattr(model_registry, "model_dir", lambda *a: d + "/models")
    import sqlite3, types, logging, os, json, numpy as np, cv2
    from modules.model_broker import NoProviderError
    from modules.personal_iqa import module as piqa, net
    import model_registry

    E = 16
    caps = {
        "embed": lambda img: np.ones(E, np.float32) * img.mean() / 255,
        "detect": lambda img: [{"class_name": "boat", "cx": .4, "cy": .5, "w": .3, "h": .3, "conf": .9},
                               {"class_name": "sky", "cx": .5, "cy": .1, "w": 1, "h": .2, "conf": .5}],
        "depth": lambda img: np.linspace(0, 1, img.shape[0])[:, None].repeat(img.shape[1], 1),
        "segment": lambda img: [{"class_name": "mountain", "mask": [(0, 1), (1, 1), (.5, .3)]}],
        "classify": lambda img: [{"class_name": "photo", "conf": .9}],
        "embed.text": lambda t: np.full(8, len(t) / 10, np.float32),
        "iqa": lambda img: {"quality": .6},
        "detect.faces": lambda img: [],
        "pose": lambda img: [],
    }
    def request_model(cap, provider=None):
        if cap in caps: return caps[cap]
        raise NoProviderError(cap)
    db = sqlite3.connect(":memory:", check_same_thread=False); db.row_factory = sqlite3.Row
    db.executescript("""CREATE TABLE files(rel_path TEXT PRIMARY KEY, mtime REAL, tags TEXT, face_done INT, autotag_done INT);
    CREATE TABLE ratings(rel_path TEXT PRIMARY KEY, user_stars INT);
    CREATE TABLE face_regions(rel_path TEXT, shape BLOB, cx REAL, cy REAL, w REAL, h REAL, not_face INT);""")
    os.makedirs(f"{d}/media", exist_ok=True)
    img = np.random.randint(0, 255, (96, 128, 3), np.uint8)
    cv2.imwrite(f"{d}/media/a.jpg", img)
    db.execute("INSERT INTO files VALUES('a.jpg', 1.0, ?, 1, 1)", (json.dumps(["cat", "sunset"]),))
    core = types.SimpleNamespace(object_grouping=types.SimpleNamespace(downscale_to_cap=lambda x, **k: x),
                                 read_image=cv2.imread, to_bgr=lambda x: x, db_close=lambda: None,
                                 read_metadata=lambda fp: {"pose": {"people": []}, "description": "a boat at dusk"},
                                 embed_faces=lambda *a, **k: ([], [], []))
    class Broker:
        def selected_id(self, c, role=None): return c if c in caps else None
        def providers_for(self, c): return []
    svc = {}; startup = []
    host = types.SimpleNamespace(core=core, config={"personal_iqa_base": "brisque", "personal_iqa_encoder": ""},
        db=lambda: db, broker=Broker(), request_model=request_model, logger=logging.getLogger("t"),
        media_dir=f"{d}/media", safe_path=lambda d, r: os.path.join(d, r),
        get_service=lambda n: None, provide_service=lambda n, s: svc.update(s), provide_model=lambda *a, **k: None,
        add_asset=lambda *a: None, add_settings_tab=lambda *a, **k: None, add_table=lambda d: db.executescript(d),
        add_config_key=lambda k, default=None, **kw: host.config.setdefault(k, default), add_settings_field=lambda **k: None,
        on_startup=lambda f: startup.append(f), add_route=lambda *a, **k: None, on=lambda *a: None)
    piqa.register(host); [f() for f in startup]
    fe = svc["features"](db, "a.jpg", 1.0)
    db.commit()
    for k in ("embed", "tile", "tile_raw", "object", "object_raw", "region", "region_raw", "depth", "style", "comp",
              "exif", "tag_text", "iqa"):
        assert fe[k], k
    assert fe["_missing"] == [] and fe["_processed"]["depth"] and fe["_processed"]["segment"]
    assert len(fe["object"]) == 2 and len(fe["region"]) == 4 and len(fe["tag_text"]) == 7  # 2 tags+caption+2 obj+1 seg+type
    # cache hit path returns the same structure
    fe2 = svc["features"](db, "a.jpg", 1.0)
    assert {k: len(v) for k, v in fe.items() if isinstance(v, list)} == {k: len(v) for k, v in fe2.items() if isinstance(v, list)}
    # train a tiny scorer on it
    samples = [{"feats": fe, "y": .8}, {"feats": fe, "y": .2}]
    m, met = svc["fit"](samples, samples, 16, 1, epochs=2, batch=2)
    assert m.dims["tag_text"] == 8 and m.dims["object"] == E and "val_mse" in met
    # old checkpoint (no new types) grows into new dims
    old = net.Scorer({"embed": E, "tile": E, "iqa": 1}, 16, 1)
    g = old.grow(16, 1, dims=svc["infer_dims"](samples, piqa.TOKEN_DIMS))
    f, mk, t = net.batch([fe], g.dims, "cpu")
    assert g(f, mk, t).shape == (1,)