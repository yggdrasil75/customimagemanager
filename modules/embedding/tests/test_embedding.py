"""! @file
@brief Embedding module: library embeddings through the picked embed model, and
image-to-image search finding the near-duplicate first."""
import numpy as np
import pytest
from cimtest import picked_model, post_json


def _unit(v):
    v = np.asarray(v, np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


def test_status_routes(client, ungated):
    for url in ("/api/embedding/status", "/api/embed_status"):
        r = client.get(url)
        assert r.status_code == 200, url


def test_generate_with_fake_model(client, upload, fake_model):
    rng = np.random.default_rng(0)
    fake_model("embed", lambda img, *a, **k: _unit(rng.normal(size=64)))
    a, b = upload("emb_a.png", seed=801), upload("emb_b.png", seed=802)
    assert a != b, "distinct uploads must not share a stored name"
    j = post_json(client, "/api/embedding/generate", {"filenames": [a, b], "force": True})
    assert j["success"] and j["embedded_now"] == 2, j
    assert j["total_embeddings"] >= 2, (
        f"embedded {j['embedded_now']} images but the library reports "
        f"{j['total_embeddings']} embeddings: they are being overwritten or miscounted")


def test_search_image_finds_near_dup(client, upload, app):
    picked_model(app, "embed")
    a = upload.media("near_dup_a.jpg")
    b = upload.media("near_dup_b.jpg")
    o = upload.media("no_person.jpg")
    j = post_json(client, "/api/embedding/generate", {"filenames": [a, b, o], "force": True})
    assert j["success"], j
    r = post_json(client, "/api/embedding/search_image", {"filename": a, "k": 5})
    assert r["success"], r
    ranked = [x.get("filename") or x.get("rel_path") for x in r["results"]]
    ranked = [x for x in ranked if x != a]
    assert ranked and ranked[0] == b, f"near-dup not ranked first: {ranked}"


def test_search_survives_mixed_dimensions(app):
    """! @brief Rows embedded by a previous model with another vector size must not
    break search (regression: 'buffer is smaller than requested size')."""
    import numpy as np, time
    db = app._db()
    svc = app.module_host.get_service("embedding")
    db.execute("DELETE FROM image_embeddings")
    now = time.time()
    cur = svc["embed_tag"]()
    v768 = (np.ones(768) / np.sqrt(768)).astype(np.float32); v512 = (np.ones(512) / np.sqrt(512)).astype(np.float32)
    db.execute("INSERT INTO image_embeddings(rel_path,dim,vec,model,mtime,updated) VALUES (?,?,?,?,?,?)",
               ("old.jxl", 512, v512.tobytes(), cur, now, now))
    db.execute("INSERT INTO image_embeddings(rel_path,dim,vec,model,mtime,updated) VALUES (?,?,?,?,?,?)",
               ("new.jxl", 768, v768.tobytes(), cur, now, now))
    db.execute("INSERT INTO image_embeddings(rel_path,dim,vec,model,mtime,updated) VALUES (?,?,?,?,?,?)",
               ("broken.jxl", 768, b"\0" * 10, cur, now, now))
    db.commit()
    try:
        hits = svc["search_by_vector"](db, v768, top_k=10)
        assert [h[0] for h in hits] == ["new.jxl"]
        assert svc["search_by_vector"](db, np.ones(64, np.float32), top_k=10) == []
    finally:
        db.execute("DELETE FROM image_embeddings"); db.commit()


def test_multiple_models_coexist(app, client):
    """! @brief Switching models must not throw away the previous model's vectors:
    rows are keyed by (image, model) and every read is scoped to the picked model."""
    import numpy as np, time
    db = app._db(); svc = app.module_host.get_service("embedding")
    db.execute("DELETE FROM image_embeddings"); now = time.time()
    cur = svc["embed_tag"]()
    v = (np.ones(512) / np.sqrt(512)).astype(np.float32)
    for model, dim in ((cur, 512), ("other-model:big", 768)):
        vec = (np.ones(dim) / np.sqrt(dim)).astype(np.float32)
        db.execute("INSERT INTO image_embeddings(rel_path,dim,vec,model,mtime,updated) VALUES (?,?,?,?,?,?)",
                   ("same.jxl", dim, vec.tobytes(), model, now, now))
    db.commit()
    try:
        assert db.execute("SELECT COUNT(*) FROM image_embeddings WHERE rel_path='same.jxl'").fetchone()[0] == 2
        assert svc["embedding_count"](db) == 1                         # only the picked model counts
        assert {m["model"] for m in svc["stored_models"](db)} == {cur, "other-model:big"}
        assert [h[0] for h in svc["search_by_vector"](db, v, top_k=5)] == ["same.jxl"]
        st = client.get("/api/embedding/status").get_json()
        assert st["total"] == 1 and len(st["stored_models"]) == 2 and st["stored_matches"]
    finally:
        db.execute("DELETE FROM image_embeddings"); db.commit()


def test_semantic_list_ranks_by_score_cuts_tail_and_honours_negatives(app, monkeypatch):
    """! @brief Regression: results were ordered by filename, so the best match could be
    on page 5. Also: the relevance cutoff drops the tail; '-term' demotes."""
    import numpy as np, time
    db = app._db(); svc = app.module_host.get_service("embedding")
    cur = svc["embed_tag"]()
    # 3-d toy space: axis 0 = "man", axis 1 = "woman", axis 2 = "dog"
    axes = {"man": [1, 0, 0], "woman": [0, 1, 0], "dog": [0, 0, 1]}
    lib = {"a_woman.jxl": [0.1, 1, 0], "b_dog.jxl": [0, 0, 1], "c_man.jxl": [1, 0.1, 0],
           "d_man_and_woman.jxl": [1, 0.9, 0], "e_manlike_dog.jxl": [0.8, 0, 0.6]}
    db.execute("DELETE FROM image_embeddings"); now = time.time()
    for n, v in lib.items():
        v = np.asarray(v, np.float64); v = (v / np.linalg.norm(v)).astype(np.float32)
        db.execute("INSERT INTO image_embeddings(rel_path,dim,vec,model,mtime,updated) VALUES (?,?,?,?,?,?)",
                   (n, 3, v.tobytes(), cur, now, now))
        db.execute("INSERT OR IGNORE INTO files(rel_path, tags, sha256, media_kind) VALUES (?, '[]', ?, 'image')", (n, n))
    db.commit()
    import modules.embedding as emb_mod
    text = lambda t: np.asarray(axes[t.split()[0]], np.float32)
    # patch the module-level resolver the list uses
    monkeypatch.setitem(app.state, "semantic_relative_cutoff", 0.0)
    orig = svc["semantic_list"].__globals__ if hasattr(svc["semantic_list"], "__globals__") else None
    try:
        closure_cells = {c.cell_contents.__name__: c for c in svc["semantic_list"].__closure__ if callable(getattr(c, "cell_contents", None)) and hasattr(c.cell_contents, "__name__")}
        cell = closure_cells["_text_embedder"]; saved = cell.cell_contents
        cell.cell_contents = lambda handle=None: text
        files, total, err = svc["semantic_list"]("man", 0, 10)
        assert err is None and files[0]["filename"] in ("c_man.jxl", "d_man_and_woman.jxl")
        scores = [f["score"] for f in files]
        assert scores == sorted(scores, reverse=True)                 # ranked, not alphabetical
        assert files[-1]["filename"] in ("a_woman.jxl", "b_dog.jxl")
        # negatives: the man+woman image drops below the pure man image
        files, _, _ = svc["semantic_list"]("man -woman", 0, 10)
        order = [f["filename"] for f in files]
        assert order.index("c_man.jxl") < order.index("d_man_and_woman.jxl")
        # cutoff keeps only the close hits
        app.state["semantic_relative_cutoff"] = 0.75
        files, total, _ = svc["semantic_list"]("man", 0, 10)
        assert {f["filename"] for f in files} <= {"c_man.jxl", "d_man_and_woman.jxl", "e_manlike_dog.jxl"}
        assert "a_woman.jxl" not in {f["filename"] for f in files} and total == len(files)
    finally:
        cell.cell_contents = saved
        db.execute("DELETE FROM image_embeddings")
        db.executemany("DELETE FROM files WHERE rel_path=?", [(n,) for n in lib]); db.commit()

# -- about:<text>, the semantic filter ---------------------------------------
# 3-d toy space: axis 0 = "man", axis 1 = "woman", axis 2 = "dog". The fake
# embed provider embeds text onto these axes; image vectors are written directly.
_AXES = {"man": [1, 0, 0], "woman": [0, 1, 0], "dog": [0, 0, 1]}
_LIB = {"a_woman.png": [0.1, 1, 0], "b_dog.png": [0, 0, 1], "c_man.png": [1, 0.1, 0],
        "d_man_and_woman.png": [1, 0.9, 0], "e_manlike_dog.png": [0.8, 0, 0.6]}
ABOUT_DIR = "about_test"


@pytest.fixture
def about_lib(app, host, upload, fake_model, monkeypatch):
    """! @brief Five uploads with known vectors in a fake text+image embedding space."""
    import time

    def model(img, *a, **k):
        return _unit(np.ones(3))
    model.space = "about-test-space"
    model.embed_text = lambda t: _unit(sum(np.asarray(_AXES[w], np.float32) for w in t.split()))
    fake_model("embed", model)
    for k in ("semantic_filter_threshold", "semantic_filter_top", "semantic_relative_cutoff",
              "semantic_min_score"):
        monkeypatch.setitem(app.state, k, 0)
    db = host.db()
    out, now = {}, time.time()
    for i, (name, vec) in enumerate(_LIB.items()):
        fn = upload(name=name, seed=870 + i, folder=ABOUT_DIR)
        out[name] = fn
        db.execute("INSERT OR REPLACE INTO image_embeddings(rel_path,dim,vec,model,mtime,updated) "
                   "VALUES (?,?,?,?,?,?)", (fn, 3, _unit(vec).tobytes(), model.space, now, now))
    db.commit()
    yield out
    db.execute("DELETE FROM image_embeddings WHERE model=?", (model.space,))
    db.commit()


def _names(client, q, **kw):
    j = client.get("/api/list", query_string={"q": q, "folder": ABOUT_DIR, **kw}).get_json()
    assert j["success"], j
    return [f["filename"] for f in j["files"]], j["total"]


def test_about_keeps_only_files_above_threshold(app, client, about_lib):
    L = about_lib
    app.state["semantic_filter_threshold"] = 0.7          # cos("man", x) >= 0.7
    names, total = _names(client, "about:man")
    assert set(names) == {L["c_man.png"], L["d_man_and_woman.png"], L["e_manlike_dog.png"]}
    assert total == 3
    assert names == sorted(names)                          # normal order, not ranked
    app.state["semantic_filter_threshold"] = 0.9
    assert set(_names(client, "about:man")[0]) == {L["c_man.png"]}
    # -about: is the complement
    assert set(_names(client, "-about:man")[0]) == set(L.values()) - {L["c_man.png"]}


def test_about_composes_with_tokens_sort_and_select_all(app, client, about_lib):
    L = about_lib
    app.state["semantic_filter_threshold"] = 0.7
    stem = L["e_manlike_dog.png"].rsplit("/", 1)[-1].rsplit(".", 1)[0]
    # free text narrows the semantic set further
    assert _names(client, f"about:man {stem}")[0] == [L["e_manlike_dog.png"]]
    # sort: orders it
    asc, _ = _names(client, "about:man sort:path")
    desc, _ = _names(client, "about:man sort:-path")
    assert desc == list(reversed(asc)) and len(asc) == 3
    # select-all works (sem: refuses it)
    j = client.get("/api/list_all", query_string={"q": "about:man", "folder": ABOUT_DIR}).get_json()
    assert j["success"] and set(j["filenames"]) == set(asc)
    j = client.get("/api/list_all", query_string={"q": "sem:man", "folder": ABOUT_DIR}).get_json()
    assert not j["success"] and "about:" in j["error"]


def test_about_negative_terms_cap_and_auto(app, client, host, about_lib):
    L = about_lib
    app.state["semantic_filter_threshold"] = 0.5
    # "_" joins words: man+woman, and -woman demotes the images that show a woman
    both = set(_names(client, "about:man_woman")[0])
    assert L["d_man_and_woman.png"] in both and L["b_dog.png"] not in both
    no_woman = set(_names(client, "about:man_-woman")[0])
    assert L["d_man_and_woman.png"] not in no_woman and L["c_man.png"] in no_woman
    # top-N cap keeps the best scoring
    app.state["semantic_filter_top"] = 1
    assert _names(client, "about:man")[0] == [L["c_man.png"]]
    app.state["semantic_filter_top"] = 0
    # auto (0): from the query's own score distribution; the best always passes,
    # the unrelated never does
    app.state["semantic_filter_threshold"] = 0
    auto = set(_names(client, "about:dog")[0])
    assert L["b_dog.png"] in auto and L["a_woman.png"] not in auto and L["c_man.png"] not in auto
    # cached: a second identical query does not embed the text again
    svc = host.get_service("embedding")
    calls = []
    prov = app.module_host.broker.request("embed").model
    orig = prov.embed_text
    prov.embed_text = lambda t: (calls.append(t), orig(t))[1]
    try:
        svc["semantic_filter"]("dog")
        svc["semantic_filter"]("dog")
    finally:
        prov.embed_text = orig
    assert calls == []                                      # served from the cache above
