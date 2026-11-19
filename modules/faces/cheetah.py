"""! @file
@brief insightface 2.x "cheetah" person packages, run by us: person boxes with a
body ReID vector each, bound to the face inside them.
======================================================================
Packages (downloaded and hash-checked by insightface into
models/face/insightface/models/):
  cheetah_s : pp_det_small_320 person detector, reid_0265 ReID, (SCRFD, ArcFace)
  cheetah_l : the large variants

insightface's own PersonAnalysis refuses every ONNX provider but CPU and CUDA,
which would leave AMD (MIGraphX) and Apple machines on the CPU. So we use only
its package download / validation and run the two person models ourselves on
the app's runtime:

  onnx   onnxruntime with the app's providers (MIGraphX, CUDA, ... then CPU)
  torch  the ONNX graphs converted to torch modules (onnx2torch) on the app's
         torch device - on a ROCm build of torch that is the AMD GPU
  auto   torch on ROCm when onnx2torch is installed, onnxruntime otherwise
         (Settings: Cheetah runtime)

Faces come from the recognition pack's SCRFD (the same detector as the 'scrfd'
provider, and its identity space), and each is bound to the body it sits in
the way PersonAnalysis does it: the face centre inside the body, >= 75% of the
face covered, and a clear margin over any rival pairing.
"""

import threading

import numpy as np

import model_registry
import object_grouping as og
from optional_deps import optional_import
from . import registry as facemodels

ensure_person_package, _HAVE_PKG = optional_import("insightface.model_zoo.person_package",
                                                   attr="ensure_person_package")
ort, _HAVE_ORT = optional_import("onnxruntime")
cv2, _HAVE_CV2 = optional_import("cv2")
onnx, _HAVE_ONNX = optional_import("onnx")
torch, _HAVE_TORCH = optional_import("torch")
onnx2torch_convert, _HAVE_O2T = optional_import("onnx2torch", attr="convert")

PACKS = ("cheetah_s", "cheetah_l")
RUNTIMES = ("auto", "onnx", "torch")
KEY = "faces:cheetah"
REID_MODE = "cheetah-reid"
NMS_IOU, MAX_PEOPLE = 0.6, 100
BIND_MARGIN = 0.12

_cfg = {"pack": "cheetah_s", "runtime": "auto"}
_err = {"v": ""}
_lock = threading.Lock()
# the face detector, handed in by the faces module: fn(img_bgr) -> normalised boxes with kps
_face_fn = {"v": None}


def available():
    """! @brief insightface >= 2.0 (the person packages) with onnx and onnxruntime."""
    return bool(_HAVE_PKG and _HAVE_ONNX and _HAVE_ORT and _HAVE_CV2)


def reason():
    """! @brief Why the cheetah providers are unavailable ('' when they are)."""
    if not _HAVE_PKG:
        return "needs insightface >= 2.0 (person packages): pip install -U insightface"
    if not _HAVE_ONNX:
        return "pip install onnx"
    return _err["v"]


def error():
    """! @brief Last load failure, '' when healthy."""
    return _err["v"]


def torch_ready():
    """! @brief The torch runtime can be used (torch + onnx2torch)."""
    return bool(_HAVE_TORCH and _HAVE_O2T)


def configure(pack=None, runtime=None, face_fn=None):
    """! @brief Pick the package / runtime and the face detector; a changed package or
    runtime drops the loaded instance so the next use rebuilds it."""
    pack = pack if pack in PACKS else _cfg["pack"]
    runtime = runtime if runtime in RUNTIMES else _cfg["runtime"]
    if face_fn is not None:
        _face_fn["v"] = face_fn
    if (pack, runtime) == (_cfg["pack"], _cfg["runtime"]):
        return
    _cfg.update(pack=pack, runtime=runtime)
    try:
        model_registry.unload(KEY)
    except Exception:
        pass


def resolved_runtime():
    """! @brief 'onnx' or 'torch': what `auto` means on this machine."""
    rt = _cfg["runtime"]
    if rt == "torch" and torch_ready():
        return "torch"
    if rt == "auto" and model_registry.backend() == "rocm" and torch_ready():
        return "torch"
    return "onnx"


class _OrtNet:
    """! @brief One ONNX graph on onnxruntime with the app's providers."""
    def __init__(self, path):
        """! @brief Open the session (the app's preferred providers, then CPU)."""
        self.sess = ort.InferenceSession(path, providers=model_registry.onnx_providers())
        self.names = [i.name for i in self.sess.get_inputs()]
        self.dtype = np.uint8 if self.sess.get_inputs()[0].type == "tensor(uint8)" else np.float32

    def run(self, feeds):
        """! @brief Inputs by name -> list of output arrays."""
        return self.sess.run(None, feeds)


class _TorchNet:
    """! @brief One ONNX graph converted to a torch module on the app's torch device."""
    def __init__(self, path):
        """! @brief Convert once (onnx2torch) and move to the device."""
        g = onnx.load(path)
        self.names = [i.name for i in g.graph.input]
        self.dtype = np.float32
        self.dev = torch.device(model_registry.device())
        self.mod = onnx2torch_convert(g).eval().to(self.dev)

    def run(self, feeds):
        """! @brief Inputs by name -> list of output arrays (a TopK's (values, indices)
        output collapses to its values, as onnxruntime returns it)."""
        with torch.no_grad():
            out = self.mod(*[torch.from_numpy(np.ascontiguousarray(feeds[n])).to(self.dev)
                             for n in self.names])
        out = list(out) if isinstance(out, (list, tuple)) else [out]
        return [(o[0] if isinstance(o, (list, tuple)) else o).detach().float().cpu().numpy()
                for o in out]


def _prep(desc, rgb_float):
    """! @brief (RGB * scale - mean) / std as the package declares, HWC -> 1CHW."""
    pp = desc.get("preprocessing")
    v = rgb_float
    if isinstance(pp, dict):
        v = v * float(pp.get("scale", 1.0))
        v = (v - np.asarray(pp.get("mean", 0.0), np.float32)) / np.asarray(pp.get("std", 1.0), np.float32)
    return np.ascontiguousarray(v.transpose(2, 0, 1)[None]).astype(np.float32)


def _nms(boxes, scores, iou, cap):
    """! @brief Greedy NMS on x1y1x2y2 -> kept indices, best first."""
    order = np.argsort(-scores, kind="stable")
    area = np.prod(np.maximum(boxes[:, 2:] - boxes[:, :2], 0), axis=1)
    keep = []
    while order.size and len(keep) < cap:
        i = int(order[0])
        keep.append(i)
        rest = order[1:]
        lt = np.maximum(boxes[i, :2], boxes[rest, :2])
        rb = np.minimum(boxes[i, 2:], boxes[rest, 2:])
        ov = np.prod(np.maximum(rb - lt, 0), axis=1)
        un = area[i] + area[rest] - ov
        o = np.divide(ov, un, out=np.zeros_like(ov), where=un > 0)
        order = rest[o <= iou]
    return keep


class Runner:
    """! @brief The cheetah person detector + ReID on one runtime."""
    def __init__(self, pack, runtime):
        """! @brief Fetch / validate the package and open its two person models."""
        tasks = ensure_person_package(pack, root=facemodels.INSIGHT_DIR)["tasks"]
        self.det_desc, self.reid_desc = tasks["person_detection"], tasks["person_reid"]
        net = _TorchNet if runtime == "torch" else _OrtNet
        self.det = net(self.det_desc["path"])
        self.reid_net = net(self.reid_desc["path"])
        self.runtime = runtime

    def persons(self, bgr, min_score=0.5):
        """! @brief Person boxes [x1, y1, x2, y2, score] in pixels (the package's own
        resize and normalisation; the graph decodes to original pixels)."""
        H, W = bgr.shape[:2]
        h, w = self.det_desc["input_size"]
        sy, sx = h / float(H), w / float(W)
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        im = cv2.resize(rgb, None, fx=sx, fy=sy, interpolation=cv2.INTER_CUBIC).astype(np.float32)
        x = _prep(self.det_desc, im)
        if self.det.dtype == np.uint8:
            x = np.ascontiguousarray(im.transpose(2, 0, 1)[None]).astype(np.uint8)
        feeds = dict(zip(self.det.names, (x, np.asarray([[sy, sx]], np.float32))))
        boxes, scores = self.det.run(feeds)[:2]
        boxes, scores = np.asarray(boxes)[0], np.asarray(scores).reshape(-1)
        ok = (scores >= min_score) & np.all(boxes[:, 2:] > boxes[:, :2], axis=1)
        boxes, scores = boxes[ok], scores[ok]
        keep = _nms(boxes, scores, NMS_IOU, MAX_PEOPLE)
        boxes, scores = boxes[keep].copy(), scores[keep]
        boxes[:, [0, 2]] = np.clip(boxes[:, [0, 2]], 0, W)
        boxes[:, [1, 3]] = np.clip(boxes[:, [1, 3]], 0, H)
        good = np.all(boxes[:, 2:] > boxes[:, :2], axis=1)
        return np.column_stack((boxes[good], scores[good])).astype(np.float32)

    def reid(self, crop_bgr):
        """! @brief L2-normalised 256-d body vector of one person crop, or None."""
        if crop_bgr is None or min(crop_bgr.shape[:2]) < 16:
            return None
        h, w = self.reid_desc["input_size"]
        im = cv2.resize(crop_bgr, (w, h), interpolation=cv2.INTER_LINEAR)[:, :, ::-1].astype(np.float32)
        v = np.asarray(self.reid_net.run({self.reid_net.names[0]: _prep(self.reid_desc, im)})[0],
                       np.float32).reshape(-1)
        n = float(np.linalg.norm(v))
        return v / n if np.isfinite(n) and n > 1e-12 else None


def _build():
    """! @brief A ready Runner for the configured package / runtime, or None (reason in error())."""
    if not available():
        _err["v"] = reason()
        return None
    rt = resolved_runtime()
    try:
        r = Runner(_cfg["pack"], rt)
        _err["v"] = ""
        return r
    except Exception as e:
        if rt == "torch":                      # a graph torch can't take: onnxruntime still can
            try:
                r = Runner(_cfg["pack"], "onnx")
                _err["v"] = f"{_cfg['pack']}: torch failed ({type(e).__name__}: {e}); on onnxruntime"
                return r
            except Exception as e2:
                e = e2
        _err["v"] = f"{_cfg['pack']}: {type(e).__name__}: {e}"
        return None


model_registry.register(KEY, _build, cost_mb=400, gpu=og.has_gpu())


def _load():
    """! @brief The runner via the central registry (loaded on demand, evictable)."""
    return model_registry.acquire(KEY)


def _norm_box(b, W, H):
    """! @brief Pixel x1,y1,x2,y2 -> normalised centre-form box."""
    x1, y1, x2, y2 = [float(v) for v in b[:4]]
    x1, x2 = max(0.0, x1 / W), min(1.0, x2 / W)
    y1, y2 = max(0.0, y1 / H), min(1.0, y2 / H)
    return {"cx": (x1 + x2) / 2, "cy": (y1 + y2) / 2, "w": x2 - x1, "h": y2 - y1}


def _xyxy(b):
    """! @brief Normalised centre-form -> x1, y1, x2, y2."""
    return (b["cx"] - b["w"] / 2, b["cy"] - b["h"] / 2, b["cx"] + b["w"] / 2, b["cy"] + b["h"] / 2)


def bind_faces(persons, faces, margin=BIND_MARGIN):
    """! @brief Which face belongs to which body (PersonAnalysis' rule): the face centre
    inside the body, >= 75% of the face covered by it, the face not huge for the
    body; best score first, and a pairing a rival comes within `margin` of is left
    unbound (ambiguous beats wrong). @return {person index: face index}."""
    cand = []
    for fi, f in enumerate(faces):
        fx1, fy1, fx2, fy2 = _xyxy(f)
        fa = max(1e-9, f["w"] * f["h"])
        for pi, p in enumerate(persons):
            px1, py1, px2, py2 = _xyxy(p)
            if not (px1 <= f["cx"] <= px2 and py1 <= f["cy"] <= py2):
                continue
            ov = max(0.0, min(fx2, px2) - max(fx1, px1)) * max(0.0, min(fy2, py2) - max(fy1, py1))
            cover = ov / fa
            if cover < 0.75 or fa > 1.5 * p["w"] * p["h"]:
                continue
            off = abs(f["cx"] - p["cx"]) / max(1e-9, p["w"])
            cand.append((0.75 * cover + 0.25 * max(0.0, 1.0 - 2.0 * off), fi, pi))
    used_f, used_p, out = set(), set(), {}
    for score, fi, pi in sorted(cand, reverse=True):
        if fi in used_f or pi in used_p:
            continue
        rivals = [s for s, f2, p2 in cand if (f2 == fi) != (p2 == pi)]
        if any(score - r < margin for r in rivals):
            continue
        out[pi] = fi
        used_f.add(fi)
        used_p.add(pi)
    return out


def analyze(img_bgr, min_score=0.5, faces=None):
    """! @brief People and faces in one image.
    @return {"faces": [{cx,cy,w,h,conf,kps}], "persons": [{cx,cy,w,h,conf,_reid,_face_box}]}
            where a person's `_face_box` is the face bound to that body (or None) and
            `_reid` its L2-normalised body vector (a list of floats, so the box stays
            JSON-able). Empty lists when unavailable.
    @param faces  face boxes already found in this image (the person pipeline's face
                  step): bind to those instead of running the face detector again.
    """
    out = {"faces": [], "persons": []}
    img = og.as_bgr(img_bgr)
    if img is None:
        return out
    r = _load()
    if r is None:
        return out
    H, W = img.shape[:2]
    with _lock:
        rows = r.persons(img, min_score)
        persons = []
        for row in rows:
            pb = _norm_box(row, W, H)
            x1, y1, x2, y2 = np.floor(row[:2]).astype(int).tolist() + np.ceil(row[2:4]).astype(int).tolist()
            v = r.reid(img[y1:y2, x1:x2])
            pb.update({"class_name": "person", "conf": float(row[4]),
                       "_reid": None if v is None else v.round(6).tolist()})
            persons.append(pb)
    if faces is not None:
        faces = [dict(f) for f in faces]
    elif _face_fn["v"] is not None:
        try:
            faces = [dict(f) for f in _face_fn["v"](img) or []]
        except Exception as e:
            _err["v"] = f"faces: {type(e).__name__}: {e}"
            faces = []
    else:
        faces = []
    pairs = bind_faces(persons, faces)
    for pi, p in enumerate(persons):
        p["_face_box"] = dict(faces[pairs[pi]]) if pi in pairs else None
    out.update(faces=faces, persons=persons)
    return out


def reid(img_bgr, boxes):
    """! @brief Body vectors for given person boxes (one ReID pass per crop, no detector).
    @return (vectors, REID_MODE); None for a box too small to embed."""
    img = og.as_bgr(img_bgr)
    r = _load() if img is not None else None
    if r is None:
        return [None] * len(boxes or []), "none"
    H, W = img.shape[:2]
    vecs = []
    for b in boxes or []:
        x1 = int(max(0, (b["cx"] - b["w"] / 2) * W)); y1 = int(max(0, (b["cy"] - b["h"] / 2) * H))
        x2 = int(min(W, (b["cx"] + b["w"] / 2) * W)); y2 = int(min(H, (b["cy"] + b["h"] / 2) * H))
        with _lock:
            vecs.append(r.reid(img[y1:y2, x1:x2]))
    return vecs, REID_MODE