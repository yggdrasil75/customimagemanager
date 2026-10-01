"""Dedup module: grouping end to end, the hash search, the naive fallback,
the scorer registry, progress / background scans, scorer-tagged cache, and
the metadata compare.
(Upload-time dedupe is sha256-only and is tested in tests/test_api.py.)"""
import os
import time
import types
import logging

import numpy as np
import pytest
from cimtest import post_json

cv2 = pytest.importorskip("cv2")
from modules.dedup import dedup_endpoints as de
from modules.dedup.module import ScorerRegistry


def _get(client, url, **qs):
    r = client.get(url, query_string=qs)
    if r.status_code == 503:
        pytest.skip(f"{url}: machine gate says {r.get_json().get('error')!r}")
    return r


# ── end to end ────────────────────────────────────────────────────────────────
def test_near_duplicate_pair_grouped(client, upload):
    a = upload.media("near_dup_a.jpg")
    b = upload.media("near_dup_b.jpg")
    o = upload.media("no_person.jpg")
    try:
        j = post_json(client, "/api/dedup", {"force": True})
        assert j["success"], j
        groups = client.get("/api/dedup_groups", query_string={"page": 0, "page_size": 200}).get_json()
        assert groups["success"]
        found = [{it["filename"] for it in g["items"]} for g in groups["groups"]]
        assert any({a, b} <= g for g in found), f"near-dup pair not grouped: {found}"
        assert not any({a, o} <= g for g in found), "unrelated photo grouped with near_dup_a"
        assert all(g["kind"] != "pending" for g in groups["groups"]), "unverified candidates leaked to the UI"
    finally:
        client.post("/api/dedup_clear", json={})


def test_status_and_clear(client):
    assert client.get("/api/dedup_status").status_code in (200, 503)
    r = client.post("/api/dedup_clear", json={})
    assert r.status_code in (200, 503)


def test_cached_result_is_tied_to_scorer(client, upload):
    upload(seed=11); upload(seed=12)
    try:
        j = post_json(client, "/api/dedup", {"force": True})
        assert j["success"], j
        again = post_json(client, "/api/dedup", {})
        assert again["success"], again
        if again.get("from_cache"):                  # only served when the same scorer made it
            assert again.get("scorer"), again
        cp = de.core.checkpoint_get()
        assert cp is not None and cp["stage"] == "verified"
        assert "scorer" in cp.keys() and cp["scorer"], "checkpoint does not record the scorer"
    finally:
        client.post("/api/dedup_clear", json={})


# ── progress / background ─────────────────────────────────────────────────────
def test_progress_endpoint_idle_and_after_sync_run(client, upload):
    upload(seed=21)
    try:
        p = _get(client, "/api/dedup_progress").get_json()
        assert "running" in p and p["stages"] == de.DEDUP_STAGES
        j = post_json(client, "/api/dedup", {"force": True})
        assert j["success"], j
        p = _get(client, "/api/dedup_progress").get_json()
        assert p["running"] is False
        assert p["result"] and p["result"]["success"]
    finally:
        client.post("/api/dedup_clear", json={})


def test_background_scan_reports_progress_and_result(client, upload):
    upload(seed=31); upload(seed=32)
    try:
        j = post_json(client, "/api/dedup", {"force": True, "background": True})
        assert j["success"] and j["running"], j
        stages, deadline = set(), time.time() + 180
        while True:
            p = _get(client, "/api/dedup_progress").get_json()
            stages.add(p.get("stage"))
            assert 0 <= (p.get("done") or 0) <= max(p.get("total") or 0, p.get("done") or 0)
            if not p["running"]:
                break
            assert time.time() < deadline, f"background dedup did not finish: {p}"
            time.sleep(0.2)
        assert p["result"] and p["result"]["success"], p
        assert p["elapsed_s"] >= 0
        # a second start while one runs is refused rather than doubled
        j1 = post_json(client, "/api/dedup", {"force": True, "background": True})
        j2 = post_json(client, "/api/dedup", {"force": True, "background": True})
        assert j1["running"] and (j2.get("started") is False or j2.get("running"))
        deadline = time.time() + 180
        while _get(client, "/api/dedup_progress").get_json()["running"]:
            assert time.time() < deadline
            time.sleep(0.2)
    finally:
        client.post("/api/dedup_clear", json={})


# ── metadata compare ──────────────────────────────────────────────────────────
def test_compare_meta_rows(client, upload):
    a, b = upload(seed=41), upload(seed=42)
    r = client.post("/api/dedup_compare_meta", json={"a": a, "b": b})
    if r.status_code == 503:
        pytest.skip("machine gate")
    assert r.status_code == 200, r.get_data(as_text=True)
    d = r.get_json()
    assert d["success"], d
    assert d["errors"] == [], d["errors"]
    assert set(d["tags"]) == {"common", "only_a", "only_b"}
    fields = {(x["section"], x["field"]): x for x in d["rows"]}
    assert ("File", "SHA-256") in fields and not fields[("File", "SHA-256")]["same"]
    assert ("File", "Filename") in fields and not fields[("File", "Filename")]["same"]
    assert ("File", "Resolution") in fields
    assert d["differ"] == sum(1 for x in d["rows"] if not x["same"]) and d["total"] == len(d["rows"])
    for x in d["rows"]:
        assert set(x) == {"section", "field", "a", "b", "same"}
        assert x["a"] is None or isinstance(x["a"], str)


def test_compare_meta_same_file_has_no_differences(client, upload):
    a = upload(seed=43)
    d = client.post("/api/dedup_compare_meta", json={"a": a, "b": a}).get_json()
    assert d["success"] and d["differ"] == 0, [x for x in d["rows"] if not x["same"]]


def test_compare_meta_missing_file(client, upload):
    a = upload(seed=44)
    r = client.post("/api/dedup_compare_meta", json={"a": a, "b": "nope/does_not_exist.jxl"})
    if r.status_code == 503:
        pytest.skip("machine gate")
    assert r.status_code in (400, 404)
    assert r.get_json()["success"] is False


def test_meta_str_handles_awkward_values():
    assert de._meta_str(None) is None
    assert de._meta_str(b"\x00" * 7) == "<7 bytes>"
    assert de._meta_str({1: "a", "b": 2}).startswith("{")          # mixed key types
    assert de._meta_str("x" * 400).endswith("(+100 chars)")
    assert de._meta_str([1, b"ab", "c"]) == "1, <2 bytes>, c"
    assert de._blank(None) and de._blank("") and de._blank([]) and not de._blank(0)
    assert not de._blank(np.zeros(3))                              # no ambiguous-truth error


# ── hash search ───────────────────────────────────────────────────────────────
def _brute(H, thr):
    bits = np.unpackbits(H, axis=1).astype(np.int32)
    n = len(H)
    out = set()
    for i in range(n):
        d = (bits[i + 1:] ^ bits[i]).sum(1)
        for j in np.nonzero(d <= thr)[0]:
            out.add((i, i + 1 + int(j)))
    return out


def test_multi_index_hash_search_matches_brute_force():
    rng = np.random.default_rng(0)
    H = rng.integers(0, 256, (600, 8), np.uint8)
    for k in range(0, 200, 2):                                     # planted near pairs, 0..5 bits apart
        H[k + 1] = H[k]
        for bit in rng.choice(64, size=int(rng.integers(0, 6)), replace=False):
            H[k + 1, bit // 8] ^= np.uint8(1 << (bit % 8))
    H[300:340] = H[300]                                            # one degenerate bucket (blank images)
    blobs = [bytes(r) for r in H]
    got = de._find_similar_pairs(blobs, 5)
    assert got.dtype == np.int64 and got.ndim == 2 and got.shape[1] == 2
    assert {tuple(p) for p in got.tolist()} == _brute(H, 5)
    assert np.all(got[:, 0] < got[:, 1])


def test_hash_search_progress_and_edge_cases():
    calls = []
    blobs = [bytes(8), bytes(8)]
    got = de._find_similar_pairs(blobs, 5, progress=lambda d, t: calls.append((d, t)))
    assert got.tolist() == [[0, 1]] and calls[-1] == (6, 6)
    assert de._find_similar_pairs([bytes(8)], 5).shape == (0, 2)
    assert de._find_similar_pairs([], 5).shape == (0, 2)


def test_pair_hamming_and_components():
    rng = np.random.default_rng(1)
    H = rng.integers(0, 256, (50, 128), np.uint8)
    pairs = np.array([[0, 1], [2, 3], [4, 49]], np.int64)
    d = de._pair_hamming([bytes(r) for r in H], pairs)
    bits = np.unpackbits(H, axis=1).astype(np.int32)
    assert d.tolist() == [int((bits[a] ^ bits[b]).sum()) for a, b in pairs]
    comps = sorted(sorted(c) for c in de._components(6, [(0, 1), (1, 2), (4, 5)]))
    assert comps == [[0, 1, 2], [4, 5]]


# ── naive fallback (full resolution, per cell) ────────────────────────────────
def _figure(angle_shift=0, h=900, w=600):
    """A figure on a pure white background; angle_shift moves its parts the
    way a camera orbit would."""
    img = np.full((h, w, 3), 255, np.uint8)
    cx = w // 2 + angle_shift
    cv2.ellipse(img, (cx, 180), (45, 60), 0, 0, 360, (60, 50, 40), -1)            # head
    cv2.rectangle(img, (cx - 70, 250), (cx + 70 - angle_shift // 2, 600), (30, 30, 160), -1)
    cv2.rectangle(img, (cx - 60, 600), (cx - 15, 860), (40, 40, 40), -1)
    cv2.rectangle(img, (cx + 15 - angle_shift, 600), (cx + 60 - angle_shift, 860), (40, 40, 40), -1)
    cv2.putText(img, "A", (cx - 20, 450), cv2.FONT_HERSHEY_SIMPLEX, 2, (255, 255, 0), 4)
    return img


def test_naive_identical_is_one_and_bytewise():
    a = _figure()
    assert de._naive_image_score(a, a.copy()) == 1.0


def test_naive_smaller_copy_is_not_punished():
    a = _figure()
    small = cv2.resize(a, (300, 450), interpolation=cv2.INTER_AREA)
    ok, enc = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 90])
    small = cv2.imdecode(enc, cv2.IMREAD_COLOR)
    assert de._naive_image_score(a, small) >= 0.8
    assert de._naive_image_score(small, a) >= 0.8                   # order does not matter


def test_naive_rotated_subject_on_white_is_not_a_duplicate():
    """The white background must not dilute the change in the subject."""
    a, b = _figure(0), _figure(60)
    assert de._naive_image_score(a, b) < 0.5


def test_naive_ctx_scorer_images_and_video():
    a = _figure()
    sc = de._naive_ctx_scorer()
    assert sc({"is_video": False, "ref_bgr": a, "other_bgr": a}) == 1.0
    assert sc({"is_video": True, "ref_frames": [a, a], "other_frames": [a, a]}) == 1.0
    assert sc({"is_video": True, "ref_frames": [], "other_frames": []}) == 0.0


# ── scorer registry ───────────────────────────────────────────────────────────
def test_registry_records_failures_and_uses_naive_callable():
    reg = ScorerRegistry()
    reg.logger = logging.getLogger("t")
    calls = {"batch": 0, "single": 0}

    def boom_group(imgs):
        raise RuntimeError("no trained checkpoint for size 'custom'")

    def boom_batch(ctxs):
        calls["batch"] += 1
        raise RuntimeError("no trained checkpoint for size 'custom'")

    def single(ctx):
        calls["single"] += 1
        return None

    reg.register({"id": "cnn", "priority": 20, "available": lambda: True,
                  "score_group": boom_group, "score_batch": boom_batch, "score": single})
    a = _figure()
    m, who = reg.score_group([a, a.copy(), _figure(60)], naive_score=de._naive_ctx_scorer())
    assert who == "naive"
    assert m[0, 1] == 1.0 and m[0, 2] < 0.5                         # naive pixel compare, not a constant
    assert "cnn" in reg.errors() and "size 'custom'" in reg.errors()["cnn"]
    assert calls["batch"] == 1 and calls["single"] == 0             # a failed batch is not retried per pair
    reg.clear_errors()
    assert reg.errors() == {}


def test_registry_constant_naive_still_supported():
    reg = ScorerRegistry()
    out = reg.score_pairs([{"is_video": False, "ref_bgr": None, "other_bgr": None}], naive_score=0.25)
    assert out == [(0.25, "naive")]


# ── dedup_cnn: trained sizes are recognized ───────────────────────────────────
def _fake_host(tmp_path, size):
    reg = ScorerRegistry()
    st = {"size": size, "provided": {}, "services": {}, "selected": [], "saved": 0}

    class Broker:
        _providers = {}

        def select(self, cap, prov, size=None, *a):
            st["selected"].append((cap, prov, size))
            st["size"] = size
            return True, None

        def current_selection(self):
            return {"dedup.pair": {"provider": "heurdu", "size": st["size"]}}

    broker = Broker()

    def provide_model(cap, pid, **kw):
        st["provided"] = kw
        broker._providers = {cap: {pid: types.SimpleNamespace(sizes=list(kw["sizes"]))}}

    host = types.SimpleNamespace(
        logger=logging.getLogger("t"), config={}, broker=broker,
        core=types.SimpleNamespace(models_dir=str(tmp_path / "models")),
        get_service=lambda name: reg if name == "dedup_scorers" else st["services"].get(name),
        declare_capability=lambda *a, **k: None,
        add_config_key=lambda *a, **k: None,
        provide_model=provide_model,
        model_variant=lambda cap, *a, **k: {"size": st["size"]},
        request_model=lambda cap, *a, **k: st["provided"]["loader"](),
        provide_service=lambda name, svc: st["services"].__setitem__(name, svc),
        save_config=lambda: st.__setitem__("saved", st["saved"] + 1),
    )
    os.makedirs(host.core.models_dir, exist_ok=True)
    return host, reg, st


def test_cnn_module_offers_trained_sizes_and_reports_why(tmp_path, monkeypatch):
    from modules.dedup_cnn import module as cnn_mod
    monkeypatch.setattr(cnn_mod, "PRETRAINED_DIR", str(tmp_path / "pretrained"))
    monkeypatch.setattr(cnn_mod._cnn_mod, "_HAVE_TORCH", True)        # the failure is reached before torch
    os.makedirs(tmp_path / "pretrained")
    host, reg, st = _fake_host(tmp_path, "custom")
    (tmp_path / "models" / "dup_cnn_xl.pt").write_bytes(b"x")
    (tmp_path / "pretrained" / "dup_cnn_shipped.pt").write_bytes(b"x")
    (tmp_path / "models" / "dup_cnn_xl.ckpt.pt").write_bytes(b"x")       # a build checkpoint, not a size
    cnn_mod.register(host)
    sizes = st["provided"]["sizes"]
    assert {"nano", "small", "medium", "large", "xl", "shipped"} <= set(sizes)
    assert "xl.ckpt" not in sizes
    # a size with no checkpoint anywhere and no download: the reason reaches dedup
    m, who = reg.score_group([_figure(), _figure()], naive_score=0.0)
    assert who == "naive"
    assert "custom" in reg.errors().get("cnn", ""), reg.errors()
    # newly trained size appears on reload, and the trainer's active size goes live
    (tmp_path / "models" / "dup_cnn_brandnew.pt").write_bytes(b"x")
    assert st["services"]["dedup_cnn"]["reload"]("brandnew")
    assert "brandnew" in host.broker._providers["dedup.pair"]["heurdu"].sizes
    assert st["selected"][-1] == ("dedup.pair", "heurdu", "brandnew") and st["saved"] == 1
    assert host.config["model_selection"]["dedup.pair"]["size"] == "brandnew"
    assert st["services"]["dedup_cnn"]["status"]()["size"] == "brandnew"