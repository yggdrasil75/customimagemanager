"""Generic model-provider suite.

Every provider registered with the broker (any module, any capability) is
parametrized into these tests, so a NEW MODEL IS COVERED THE MOMENT IT
REGISTERS — no edit to this file, ever. Adding a model:

    1. register it (host.provide_model(...)) as usual
    2. ./run_tests.sh tests/test_providers.py -k "pose:mymodel"
    3. if it deviates from the capability contract on purpose, say so in
       tests/model_expectations.json — data, not code.

Each run ends with a "model results" table: one line per model, ok or the
first failure, so "yolo pose works, mediapipe holistic doesn't" is readable at
a glance. Test one capability with -k "pose:", one model with -k "pose:rtmw".
By default a provider is tested with the size/type in effect for it; pass
--cim-all-variants to sweep every size and type it declares.

  test_declaration   manifest-level sanity; runs even for unavailable providers
  test_contract      output matches the capability's canonical shape
  test_blank_image   a blank frame never raises and still returns the shape
  test_conf_filter   conf= is honoured: a higher threshold never returns more
  test_batch         .batch(imgs), when offered, returns one valid result per image
  test_<cap>_*       behaviour on the real fixture media (tests/fixtures/)

A provider is skipped when it reports itself unavailable (with its reason), or
when it runs on an external endpoint and --cim-remote isn't given. A provider
that reports available() but fails to load FAILS: available() is lying.
"""
import json
import os

import numpy as np
import pytest

import cimtest
from cimtest import load_app, load_image, has_fixture, expected, text_matches

# Known, deliberate deviations live in tests/model_expectations.json so adding
# or annotating a model never means editing this file. See that file's
# "_how_to_use" key for the schema.
_EXP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model_expectations.json")


def _load_expectations():
    try:
        with open(_EXP_PATH, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return {}
    except (ValueError, OSError) as e:
        pytest.fail(f"{_EXP_PATH}: {e}")
    return {k: v for k, v in raw.items() if not k.startswith("_")}


EXPECTATIONS = _load_expectations()


def expectation(cap, pid, test_name=None):
    """The entry for this model ('cap:provider', or 'cap:*' for every provider
    of a capability), narrowed to one test when test_name is given."""
    e = dict(EXPECTATIONS.get(f"{cap}:{pid}") or EXPECTATIONS.get(f"{cap}:*") or {})
    per_test = e.pop("tests", {}) or {}
    if test_name and test_name in per_test:
        e.update(per_test[test_name] if isinstance(per_test[test_name], dict)
                 else {"xfail": per_test[test_name]})
    return e

# Capabilities the generic suite can't call without provider-specific input.
NO_GENERIC_CALL = {
    "box": "the 'box' capability binds to a weights file chosen per call, so there is "
           "nothing generic to call here; the same detectors are covered under detect/*",
    "body.mesh": "mesh(betas) takes a body-model shape vector whose length is specific to "
                 "the model (SMPL-X uses 10-300, ANNY its own): the generic suite can't "
                 "invent a valid one. Test it in the providing module's tests, where the "
                 "right betas are known.",
}

SPEEDS = {"", "fast", "balanced", "accurate"}
PROMPT = "person"


# ── parametrization ────────────────────────────────────────────────────────
def for_caps(*caps):
    """Restrict a test to providers of these capabilities (prefix match on
    'detect.' etc. is not implied — list them)."""
    def deco(fn):
        fn.caps = caps
        return fn
    return deco


def _variants(p):
    """[(size, type)] to test for a provider: the variant in effect (None,
    None = leave the broker's resolution alone) or, with --cim-all-variants,
    every combination the provider declares."""
    if not cimtest.ALL_VARIANTS:
        return [(None, None)]
    sizes = p["sizes"] or [None]
    types = [t["value"] for t in p["types"]] or [None]
    return [(s, t) for s in sizes for t in types]


def pytest_generate_tests(metafunc):
    if "prov" not in metafunc.fixturenames:
        return
    b = load_app().module_host.broker
    want = getattr(metafunc.function, "caps", None)
    items, ids = [], []
    for c in b.status():
        if want is not None and c["id"] not in want:
            continue
        for p in c["providers"]:
            if p["id"] == cimtest.FAKE_ID:
                continue
            for size, typ in _variants(p):
                items.append((c["id"], p["id"], size, typ))
                tag = "/".join(x for x in (size, typ) if x)
                ids.append(f"{c['id']}:{p['id']}" + (f"[{tag}]" if tag else ""))
    metafunc.parametrize("prov", items, ids=ids)


_LOAD_ERR = {}


@pytest.fixture
def P(prov, app, request):
    """(provider, bound handle) for a callable provider, or skip/fail.
    Applies tests/model_expectations.json and, when sweeping, pins the size /
    type for the duration of the test."""
    cap, pid, size, typ = prov
    b = app.module_host.broker
    p = b._providers[cap][pid]
    exp = expectation(cap, pid, request.node.name.split("[")[0])
    if exp.get("skip"):
        pytest.skip(f"model_expectations.json: {exp['skip']}")
    if exp.get("xfail"):
        request.node.add_marker(pytest.mark.xfail(reason=f"model_expectations.json: {exp['xfail']}",
                                                  strict=False))
    if size or typ:
        prev = dict(b._variant.get(cap, {}))
        forced = {**prev, **({"size": size} if size else {}), **({"type": typ} if typ else {})}
        b._variant[cap] = forced
        request.addfinalizer(lambda: b._variant.__setitem__(cap, prev))
    if cap in NO_GENERIC_CALL:
        pytest.skip(NO_GENERIC_CALL[cap])
    if not p.available():
        pytest.skip(f"unavailable: {p.reason()}")
    if p.resource and not cimtest.REMOTE:
        pytest.skip(f"runs on external endpoint '{p.resource}' (pass --cim-remote)")
    if prov in _LOAD_ERR:
        pytest.skip(f"model does not load (reported by test_contract[{cap}:{pid}])")
    try:
        # Not cached: the loader is LRU-backed, and holding every handle would
        # pin every model in memory at once.
        h = b.request(cap, provider=pid)
    except Exception as e:
        _LOAD_ERR[prov] = f"{type(e).__name__}: {e}"
        pytest.fail(f"{cap}:{pid} says available() is True, then fails to load: "
                    f"{_LOAD_ERR[prov]}\n"
                    f"Either available() should return False here (so the app hides "
                    f"the model instead of erroring at use time) or the weights/deps "
                    f"it needs are missing on this machine.")
    return p, h


def _exempt(p):
    """Skip a behaviour test for a model that deliberately doesn't do this."""
    r = expectation(p.capability, p.id).get("exempt")
    if r:
        pytest.skip(f"model_expectations.json: {r}")


# ── calling convention per capability ──────────────────────────────────────
CENTER_BOX = {"class_name": "person", "cx": .5, "cy": .5, "w": .6, "h": .9}


def call(p, h, img, **kw):
    cap = p.capability
    if cap in ("segment.box", "embed.faces", "embed.bodies"):
        return h(img, kw.pop("boxes", [CENTER_BOX]), **kw)
    if cap in ("face.shape", "body.shape"):
        return h(kw.pop("crops", [(img, CENTER_BOX)]))
    if p.prompted:
        return h(img, kw.pop("prompt", PROMPT), **kw)
    return h(img, **kw)


def std_image():
    """person_single.jpg when present, else a textured synthetic frame."""
    if has_fixture("person_single.jpg"):
        return load_image("person_single.jpg")
    rng = np.random.default_rng(0)
    import cv2
    return cv2.GaussianBlur(rng.integers(0, 255, (240, 320, 3), dtype=np.uint8), (9, 9), 0)


# ── validators (the canonical shapes from modules/model_contracts.py) ──────
def _num01(v, what):
    v = float(v)
    assert -1e-6 <= v <= 1 + 1e-6, f"{what}={v} outside 0..1"
    return v


def v_box(b, need_class=True, need_conf=False):
    assert isinstance(b, dict), f"box is {type(b).__name__}, not dict"
    for k in ("cx", "cy", "w", "h"):
        assert k in b, f"box missing {k}: {b}"
        _num01(b[k], k)
    assert float(b["w"]) > 0 and float(b["h"]) > 0, f"degenerate box {b}"
    if need_class:
        assert isinstance(b.get("class_name"), str) and b["class_name"], f"box needs class_name: {b}"
    if need_conf or "conf" in b:
        _num01(b.get("conf"), "conf")
    if "angle" in b:
        float(b["angle"])


def v_boxes(out, cap):
    assert isinstance(out, list), f"{cap} must return a list, got {type(out).__name__}"
    for b in out:
        v_box(b, need_class=cap != "detect.faces", need_conf=cap == "detect.faces")
    if cap == "detect.persons":
        assert all(b["class_name"] == "person" for b in out), \
            f"detect.persons class_name must be 'person': {[b['class_name'] for b in out]}"


def v_masks(out, cap):
    assert isinstance(out, list), f"{cap} must return a list"
    for m in out:
        assert isinstance(m, dict) and "mask" in m, f"mask entry needs 'mask': {str(m)[:120]}"
        assert isinstance(m.get("class_name", ""), str)
        pts = list(m["mask"])
        assert len(pts) >= 3, "polygon needs >= 3 points"
        for pt in pts:
            x, y = (pt["x"], pt["y"]) if isinstance(pt, dict) else pt
            _num01(x, "mask x"); _num01(y, "mask y")
        if "conf" in m:
            _num01(m["conf"], "conf")


def v_semantic(out, cap, img):
    assert isinstance(out, dict) and "mask" in out and "names" in out
    m = np.asarray(out["mask"])
    assert m.ndim == 2 and np.issubdtype(m.dtype, np.integer), f"mask {m.shape} {m.dtype}"
    assert isinstance(out["names"], dict)


def v_pose(out, cap):
    assert isinstance(out, list), "pose must return a list of people"
    for person in out:
        kps = person.get("keypoints")
        assert isinstance(kps, list) and kps, "person needs keypoints"
        for k in kps:
            assert {"x", "y", "v"} <= set(k), f"keypoint needs x,y,v: {k}"
            _num01(k["v"], "v")
            if float(k["v"]) > 0:                  # invisible points may sit anywhere
                _num01(k["x"], "x"); _num01(k["y"], "y")
        if "conf" in person:
            float(person["conf"])


def v_depth(out, cap, img):
    d = np.asarray(out)
    assert d.ndim == 2, f"depth must be HxW, got {d.shape}"
    assert np.issubdtype(d.dtype, np.floating), f"depth dtype {d.dtype}"
    H, W = img.shape[:2]
    assert abs(d.shape[0] / d.shape[1] - H / W) < 0.05, "depth aspect differs from input"


def v_ranked(out, cap, key):
    assert isinstance(out, list)
    for e in out:
        assert isinstance(e.get(key), str) and e[key], f"entry needs {key}: {e}"
        _num01(e["conf"], "conf")
    confs = [float(e["conf"]) for e in out]
    assert confs == sorted(confs, reverse=True), f"{cap} must be sorted by conf desc"


def v_ocr(out, cap):
    assert isinstance(out, dict) and isinstance(out.get("text", ""), str)
    for ln in out.get("lines", []) or []:
        assert isinstance(ln.get("text"), str)
        if "cx" in ln:
            v_box(ln, need_class=False)


def v_embed(out, cap, allow_none):
    if out is None:
        assert allow_none, "embed returned None on a normal image"
        return
    v = np.asarray(out)
    assert v.ndim == 1 and v.size > 0, f"embedding must be 1-D, got {v.shape}"
    assert np.issubdtype(v.dtype, np.floating)
    assert abs(float(np.linalg.norm(v)) - 1) < 1e-2, f"not L2-normalised (|v|={np.linalg.norm(v):.3f})"


def v_embed_boxes(out, cap, n):
    assert isinstance(out, tuple) and len(out) >= 2, "must return (vectors, mode[, shapes])"
    vecs, mode = out[0], out[1]
    assert len(vecs) == n, f"{len(vecs)} vectors for {n} boxes"
    assert isinstance(mode, str) and mode
    for v in vecs:
        if v is not None:
            v = np.asarray(v)
            assert v.ndim == 1 and v.size > 0


def v_iqa(out, cap, blank=False):
    assert isinstance(out, dict) and "quality" in out and "raw" in out
    if out["quality"] is None:
        assert blank, "iqa returned quality=None for a normal image (it must score it or raise)"
        return
    _num01(out["quality"], "quality")


def v_mesh(out, cap):
    if out is None:
        return
    if isinstance(out, dict):
        verts, faces = out.get("vertices"), out.get("faces")
        if verts is None and "betas" in out:
            return                                  # parametric fit: betas only
    else:
        verts, faces = out[0], out[1]
    verts, faces = np.asarray(verts), np.asarray(faces)
    assert verts.ndim == 2 and verts.shape[1] == 3, f"vertices {verts.shape}"
    if faces.size:
        assert faces.ndim == 2 and faces.shape[1] == 3 and faces.max() < len(verts)


# Outputs that routes hand straight to jsonify must be plain Python types
# (numpy scalars in a box make the whole response a 500).
JSON_CAPS = ("detect", "segment", "pose", "classify", "tag", "describe", "ocr", "iqa")


def validate(p, out, img, n_boxes=1, blank=False):
    cap = p.capability
    if cap.split(".")[0] in JSON_CAPS and cap != "segment.semantic":
        import json
        try:
            json.dumps(out)
        except TypeError as e:
            pytest.fail(f"{cap} output is not JSON-serialisable ({e}); cast numpy values to float/int")
    if cap.startswith("detect"):
        return v_boxes(out, cap)
    if cap in ("segment", "segment.box"):
        v_masks(out, cap)
        if cap == "segment.box" and not blank:
            assert len(out) == n_boxes, f"segment.box: {len(out)} masks for {n_boxes} boxes"
        return
    if cap == "segment.semantic":
        return v_semantic(out, cap, img)
    if cap == "pose":
        return v_pose(out, cap)
    if cap == "depth":
        return v_depth(out, cap, img)
    if cap == "classify":
        return v_ranked(out, cap, "class_name")
    if cap == "tag":
        return v_ranked(out, cap, "tag")
    if cap == "describe":
        assert isinstance(out, str)
        return
    if cap == "ocr":
        return v_ocr(out, cap)
    if cap == "embed":
        return v_embed(out, cap, allow_none=blank)
    if cap in ("embed.faces", "embed.bodies"):
        return v_embed_boxes(out, cap, n_boxes)
    if cap == "iqa":
        return v_iqa(out, cap, blank=blank)
    if cap in ("face.shape", "body.shape"):
        return v_mesh(out, cap)
    pytest.skip(f"no generic validator for capability '{cap}' — ship a test in the providing module")


def _count(p, out):
    return len(out) if isinstance(out, list) else 0


def _cos(a, b):
    a, b = np.asarray(a, np.float32), np.asarray(b, np.float32)
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


# ── every provider ─────────────────────────────────────────────────────────
def test_declaration(prov, app):
    cap, pid = prov[0], prov[1]
    b = app.module_host.broker
    assert b.has_capability(cap)
    p = b._providers[cap][pid]
    d = p.as_dict()
    assert d["label"], "provider needs a label"
    assert p.module_id, "provider not attributed to a module (register it from register())"
    assert p.speed in SPEEDS, f"speed {p.speed!r} not in {SPEEDS}"
    assert all(isinstance(s, str) for s in p.sizes), "sizes must be ids (str)"
    assert all(isinstance(t, dict) and "value" in t and "label" in t for t in p.types)
    if not p.available():
        assert p.reason(), "an unavailable provider must say why"


def test_contract(P):
    p, h = P
    img = std_image()
    validate(p, call(p, h, img), img)


def test_blank_image(P):
    p, h = P
    img = np.zeros((64, 64, 3), np.uint8)
    validate(p, call(p, h, img), img, blank=True)


def test_conf_filter(P):
    p, h = P
    if not p.supports_conf:
        pytest.skip("provider does not take conf=")
    name = "person_multi.jpg" if has_fixture("person_multi.jpg") else None
    img = load_image(name) if name else std_image()
    lo = _count(p, call(p, h, img, conf=0.05))
    hi = _count(p, call(p, h, img, conf=0.9))
    assert hi <= lo, f"conf=0.9 returned {hi} > conf=0.05 returned {lo}"


def test_batch(P):
    p, h = P
    if not hasattr(h, "batch"):
        pytest.skip("no .batch entry point")
    imgs = [std_image(), np.zeros((64, 64, 3), np.uint8)]
    out = h.batch(imgs)
    assert len(out) == len(imgs), f"batch returned {len(out)} results for {len(imgs)} images"
    validate(p, out[0], imgs[0])
    single = _count(p, call(p, h, imgs[0]))
    assert abs(_count(p, out[0]) - single) <= 1, "batch and single call disagree on the same image"


# ── behaviour on real fixtures ─────────────────────────────────────────────
def _confident(out, t=0.5):
    return [b for b in out if float(b.get("conf", 1.0)) >= t]


FIXTURE_HINT = ("\nIf every model fails this way, the fixture is the suspect: "
                "tests/test_fixtures.py checks it.")


@for_caps("detect.persons")
def test_persons_counts(P):
    p, h = P
    _exempt(p)
    one = call(p, h, load_image("person_single.jpg"))
    assert len(_confident(one)) == 1, (
        f"person_single: {len(_confident(one))} confident persons (want exactly 1)"
        + FIXTURE_HINT)
    top = max(one, key=lambda b: b["w"] * b["h"])
    assert top["h"] > 0.4, f"person_single: tallest person box only {top['h']:.2f} of frame"
    assert len(_confident(call(p, h, load_image("person_multi.jpg")))) >= 2
    assert _confident(call(p, h, load_image("no_person.jpg"))) == []


@for_caps("detect.faces")
def test_faces_counts(P):
    p, h = P
    _exempt(p)
    close = call(p, h, load_image("face_closeup.jpg"))
    assert close, "face_closeup: no face"
    assert max(b["w"] for b in close) > 0.2, "face_closeup: face box too small"
    assert len(call(p, h, load_image("person_multi.jpg"))) >= 2
    assert call(p, h, load_image("no_person.jpg")) == []


@for_caps("detect")
def test_detect_finds_person(P):
    p, h = P
    _exempt(p)
    classes = p.classes() if not p.prompted else ["person"]
    if "person" not in classes:
        pytest.skip("model has no 'person' class")
    out = call(p, h, load_image("person_single.jpg"))
    assert any(b["class_name"] == "person" for b in out), [b["class_name"] for b in out]


@for_caps("detect.barcodes")
def test_barcodes_found(P):
    p, h = P
    _exempt(p)
    for name in ("barcode_qr.png", "barcode_1d.png"):
        if has_fixture(name):
            assert call(p, h, load_image(name)), f"{name}: no barcode box"
    assert call(p, h, load_image("no_person.jpg")) == []


@for_caps("segment")
def test_segment_person(P):
    p, h = P
    _exempt(p)
    out = call(p, h, load_image("person_single.jpg"))
    assert out, "no masks on person_single" + FIXTURE_HINT
    if p.prompted:
        assert any(m.get("class_name") == PROMPT for m in out)


@for_caps("segment.box")
def test_segment_box_stays_in_box(P):
    p, h = P
    _exempt(p)
    img = load_image("person_single.jpg")
    bx = dict(CENTER_BOX)
    out = call(p, h, img, boxes=[bx])
    assert len(out) == 1
    pts = np.asarray([(q["x"], q["y"]) if isinstance(q, dict) else q for q in out[0]["mask"]], float)
    cx, cy = pts.mean(axis=0)
    assert abs(cx - bx["cx"]) < bx["w"] / 2 and abs(cy - bx["cy"]) < bx["h"] / 2, \
        "mask centroid outside its prompt box"


@for_caps("pose")
def test_pose_people(P):
    p, h = P
    _exempt(p)
    people = call(p, h, load_image("person_single.jpg"))
    assert people, "no skeleton on person_single" + FIXTURE_HINT
    best = max(people, key=lambda q: sum(float(k["v"]) > 0.3 for k in q["keypoints"]))
    assert len(best["keypoints"]) >= 17
    seen = sum(float(k["v"]) > 0.3 for k in best["keypoints"][:17])
    assert seen >= 12, f"only {seen}/17 body joints visible on a full-body standing person"
    assert len(call(p, h, load_image("person_multi.jpg"))) >= 2
    none = call(p, h, load_image("no_person.jpg"))
    assert not [q for q in none if sum(float(k["v"]) > 0.3 for k in q["keypoints"]) >= 6], \
        "skeleton found on no_person"


@for_caps("depth")
def test_depth_varies(P):
    p, h = P
    d = np.asarray(call(p, h, load_image("person_single.jpg")), np.float32)
    assert float(d.std()) > 1e-4, "depth map is constant"


@for_caps("classify", "tag", "describe")
def test_says_something(P):
    p, h = P
    out = call(p, h, load_image("person_single.jpg"))
    assert out, f"{p.capability} returned nothing for person_single" + FIXTURE_HINT


@for_caps("ocr")
def test_ocr_reads(P):
    p, h = P
    out = call(p, h, load_image("text_document.jpg"))
    text = " ".join(((out or {}).get("text") or "").split()).lower()
    assert text, "no text read"
    want = expected("text_document.jpg")
    if want:
        ok, detail = text_matches(want, text)
        assert ok, f"text_document expectation not met ({detail}); read: {text[:300]}"
    empty = " ".join(((call(p, h, load_image("no_person.jpg")) or {}).get("text") or "").split())
    assert len(empty) < 20, f"read text on no_person: {empty!r}"


@for_caps("embed")
def test_embed_near_dup_closer(P):
    p, h = P
    a = call(p, h, load_image("near_dup_a.jpg"))
    b = call(p, h, load_image("near_dup_b.jpg"))
    o = call(p, h, load_image("no_person.jpg"))
    assert _cos(a, b) > _cos(a, o), f"near-dup {_cos(a, b):.3f} not closer than unrelated {_cos(a, o):.3f}"


def _face_box(app, img):
    try:
        faces = app.module_host.broker.request("detect.faces")(img) or []
    except Exception as e:
        pytest.skip(f"no working detect.faces pick to find face boxes: {e}")
    if not faces:
        pytest.skip("detect.faces found no face on an identity fixture")
    return max(faces, key=lambda f: f["w"] * f["h"])


@for_caps("embed.faces")
def test_face_identity(P, app):
    p, h = P
    vecs = {}
    for n in ("same_person_a.jpg", "same_person_b.jpg", "other_person.jpg"):
        img = load_image(n)
        out = h(img, [_face_box(app, img)])
        if out[1] == "appearance":
            pytest.skip("appearance mode carries no identity")
        assert out[0][0] is not None, f"no embedding for {n}"
        vecs[n] = out[0][0]
    same = _cos(vecs["same_person_a.jpg"], vecs["same_person_b.jpg"])
    other = _cos(vecs["same_person_a.jpg"], vecs["other_person.jpg"])
    assert same > other, f"same person {same:.3f} not closer than other person {other:.3f}"


@for_caps("iqa")
def test_iqa_prefers_sharp(P):
    p, h = P
    import cv2
    img = load_image("person_single.jpg")
    sharp = call(p, h, img)["quality"]
    blurred = call(p, h, cv2.GaussianBlur(img, (0, 0), 6))["quality"]
    if sharp is None or blurred is None:
        pytest.fail(f"{p.id} scored quality=None on a real photo "
                    f"(sharp={sharp}, blurred={blurred})")
    assert float(sharp) > float(blurred), (
        f"{p.id} rates a heavily blurred copy ({float(blurred):.3f}) at or above the "
        f"sharp original ({float(sharp):.3f}) — fine for an aesthetic metric, wrong for "
        f"one used to pick the best shot")
