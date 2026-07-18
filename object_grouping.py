"""
object_grouping.py
==================
Untrained-object discovery, depth-aware embedding, and cross-image grouping
for the AI Media & Asset Manager.

WHAT THIS IS FOR
----------------
YOLO can only box classes it was trained on. A lot of everyday objects (a cup,
a mug, a particular prop) were never in the training set. This module finds
those objects *without* a trained detector, describes each one with a compact
embedding, and groups visually-similar objects ACROSS many images so the user
can bulk-tag a whole cluster at once ("these 40 boxes are all 'cup of orange
juice'").

PIPELINE
--------
    image ->  depth map           (Depth-Anything v2, or cv2 pseudo-depth)
          ->  region proposals    (saliency + contours; no trained model)
          ->  per-region embedding (CNN if torch present, else cv2+depth+shape)
          ->  cluster embeddings across images
          ->  tag-assisted labelling of clusters

EVERYTHING DEGRADES
-------------------
The heavy paths (Depth-Anything, a CNN backbone) need torch + a GPU. When
torch is missing the module silently falls back to numpy/cv2/sklearn features,
so it is useful on a CPU box and gets sharper the moment a GPU is enabled.
Nothing here can crash the app: every public function catches and returns a
safe empty result on failure.

SIZE RULES (per the feature spec)
---------------------------------
* images smaller than MIN_IMAGE_PX on the short side are skipped entirely —
  tiny thumbnails wreck region proposals and embeddings.
* candidate boxes smaller than MIN_BOX_PX on either side are dropped — too
  little signal to be worth grouping.
"""

import os
import gc
import json
import math
import queue
import threading
import functools
import numpy as np
from concurrent.futures import ThreadPoolExecutor

try:
    import cv2
    _HAVE_CV2 = True
except Exception:
    _HAVE_CV2 = False

_DEPTH = {"loaded": False, "model": None, "proc": None, "path": None}
_CNN = {"loaded": False, "model": None, "path": None, "dim": 0}
try:
    import torch
except Exception:
    torch = None
# ── tunables ──────────────────────────────────────────────────────────────────
MIN_IMAGE_PX = 256          # skip images whose short side is below this
MAX_IMAGE_PX = 2048         # HARD cap on the LONG side; downscale before anything
                            # else. A full-res decode of a few 20k–40k px images,
                            # held across decode_workers + a gpu_batch, was the
                            # real OOM (tens of GB per image). Proposals run at
                            # _WORK=384 and CNN crops at 224, so nothing downstream
                            # benefits from more than ~2k px. This bounds per-image
                            # RAM to a few MB regardless of source resolution.
MIN_BOX_PX = 32             # drop proposal boxes below this on either side
_WORK = 384                 # working resolution for depth / proposals
_EMB_FALLBACK_DIM = 64      # cv2-feature embedding length


def downscale_to_cap(img, max_px=MAX_IMAGE_PX):
    """Downscale so the LONGEST side is <= max_px, preserving aspect ratio.
    Returns the image unchanged if already within the cap. This is the single
    most important memory guard in the scan: it must run on every image right
    after decode, before depth/proposals/crops, so no full-resolution giant is
    ever held in RAM or batched. Never raises; returns the input on any error."""
    if img is None or not _HAVE_CV2:
        return img
    try:
        h, w = img.shape[:2]
        long_side = max(h, w)
        if long_side <= max_px:
            return img
        scale = max_px / float(long_side)
        nw = max(1, int(round(w * scale)))
        nh = max(1, int(round(h * scale)))
        return cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
    except Exception:
        return img




def has_gpu():
    """True if a CUDA device is usable. Cheap and cached by torch internally."""
    if torch is None:
        return False
    try:
        return bool(torch.cuda.is_available())
    except Exception:
        return False


# ── image gating ──────────────────────────────────────────────────────────────
def image_too_small(img):
    """True if the image's short side is below MIN_IMAGE_PX (skip these)."""
    if img is None:
        return True
    h, w = img.shape[:2]
    return min(h, w) < MIN_IMAGE_PX


def _box_ok(x1, y1, x2, y2):
    return (x2 - x1) >= MIN_BOX_PX and (y2 - y1) >= MIN_BOX_PX


# ── depth ─────────────────────────────────────────────────────────────────────
def _load_depth(model_path=None):
    """Lazy-load Depth-Anything v2 via transformers. Returns True on success.
    `model_path` may be a HF id or a local dir; defaults to the small v2 model.
    Re-attempts if a *different* model is requested than the last try."""
    mid = model_path or "depth-anything/Depth-Anything-V2-Small-hf"
    if _DEPTH["loaded"] and _DEPTH.get("req") == mid:
        return _DEPTH["model"] is not None
    _DEPTH["loaded"] = True
    _DEPTH["req"] = mid
    if not _have_torch():
        _DEPTH["model"] = None
        return False
    try:
        torch = _get_torch()
        from transformers import AutoImageProcessor, AutoModelForDepthEstimation
        proc = AutoImageProcessor.from_pretrained(mid)
        model = AutoModelForDepthEstimation.from_pretrained(mid)
        model.eval()
        if torch.cuda.is_available():
            model = model.to("cuda")
        _DEPTH.update(model=model, proc=proc, path=mid)
        return True
    except Exception:
        _DEPTH.update(model=None, proc=None)
        return False


def _pseudo_depth(gray):
    """cv2-only depth proxy when no model is available. Not metric — just a
    monotonic-ish cue: near objects tend to be sharper / higher-contrast, so we
    use local gradient energy as a stand-in. Returns float32 0..1, same HxW."""
    g = gray.astype(np.float32)
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
    energy = cv2.GaussianBlur(np.hypot(gx, gy), (0, 0), 7)
    e = energy - energy.min()
    rng = float(e.max()) or 1.0
    return (e / rng).astype(np.float32)


def depth_map(img_bgr, model_path=None):
    """Return a float32 depth map (HxW, ~0..1, larger = nearer) for an image.
    Uses Depth-Anything v2 when available, else a cv2 pseudo-depth. Never raises.
    Returns None only if the input is unusable."""
    if img_bgr is None or not _HAVE_CV2:
        return None
    try:
        h, w = img_bgr.shape[:2]
        if _load_depth(model_path):
            torch = _get_torch()
            from PIL import Image
            rgb = cv2.cvtColor(img_bgr[:, :, :3], cv2.COLOR_BGR2RGB)
            pil = Image.fromarray(rgb)
            inp = _DEPTH["proc"](images=pil, return_tensors="pt")
            if torch.cuda.is_available():
                inp = {k: v.to("cuda") for k, v in inp.items()}
            with torch.no_grad():
                pred = _DEPTH["model"](**inp).predicted_depth
            d = pred.squeeze().detach().float().cpu().numpy()
            d = cv2.resize(d, (w, h), interpolation=cv2.INTER_CUBIC)
            d = d - d.min()
            rng = float(d.max()) or 1.0
            return (d / rng).astype(np.float32)
        # fallback
        gray = cv2.cvtColor(img_bgr[:, :, :3], cv2.COLOR_BGR2GRAY)
        return _pseudo_depth(gray)
    except Exception:
        try:
            gray = cv2.cvtColor(img_bgr[:, :, :3], cv2.COLOR_BGR2GRAY)
            return _pseudo_depth(gray)
        except Exception:
            return None


# ── region proposals (no trained detector) ────────────────────────────────────
def depth_map_batch(imgs_bgr, model_path=None):
    """Depth for a LIST of images in one GPU forward pass. Returns a list of
    float32 maps aligned with the input (each resized back to its own HxW).
    Falls back to per-image pseudo-depth when no model/torch. Never raises."""
    if not imgs_bgr:
        return []
    if not _load_depth(model_path):
        out = []
        for im in imgs_bgr:
            try:
                out.append(_pseudo_depth(cv2.cvtColor(im[:, :, :3], cv2.COLOR_BGR2GRAY)))
            except Exception:
                out.append(None)
        return out
    try:
        torch = _get_torch()
        from PIL import Image
        pil = [Image.fromarray(cv2.cvtColor(im[:, :, :3], cv2.COLOR_BGR2RGB))
               for im in imgs_bgr]
        inp = _DEPTH["proc"](images=pil, return_tensors="pt")
        if torch.cuda.is_available():
            inp = {k: v.to("cuda") for k, v in inp.items()}
        with torch.no_grad():
            pred = _DEPTH["model"](**inp).predicted_depth   # (N, h, w)
        pred = pred.detach().float().cpu().numpy()
        out = []
        for arr, im in zip(pred, imgs_bgr):
            h, w = im.shape[:2]
            d = cv2.resize(arr, (w, h), interpolation=cv2.INTER_CUBIC)
            d = d - d.min()
            out.append((d / (float(d.max()) or 1.0)).astype(np.float32))
        # Release torch/inp tensors and trim the CPU allocator. On a long CPU
        # run, torch's caching allocator and malloc arenas otherwise keep every
        # batch's activation buffers, pinning RSS near 100% even with no leak.
        del pred, inp, pil
        try:
            import ctypes
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except Exception:
            pass
        return out
    except Exception:
        # fall back per-image
        return [depth_map(im, model_path) for im in imgs_bgr]


# Proposal source: "heuristic" (saliency+contours, the original) or "sam"
# (Segment Anything, sharper boundaries). Set once by the discovery entrypoints
# from the `object_proposals` setting; propose_regions dispatches on it so no
# call site in discover_stages has to change. Kept module-level (not a param)
# precisely so the many existing propose_regions() callers keep working.
_PROPOSAL_SOURCE = "heuristic"


def set_proposal_source(source):
    """Select the region-proposal backend for subsequent propose_regions calls.
    'sam' routes through sam_proposals (falling back to heuristic if SAM is
    unavailable); anything else uses the built-in heuristic proposer."""
    global _PROPOSAL_SOURCE
    _PROPOSAL_SOURCE = "sam" if (source or "").lower() == "sam" else "heuristic"
    return _PROPOSAL_SOURCE


def proposal_source():
    return _PROPOSAL_SOURCE


def propose_regions(img_bgr, depth=None, max_regions=40, seed_boxes=None):
    """Find candidate object boxes without a trained detector.

    Dispatches to the configured proposal source. With the SAM source, masks
    respect object boundaries (far cleaner crops than the heuristic); YOLO
    `seed_boxes` (known-class detections) are kept verbatim and de-duplicated
    against SAM masks so YOLO covers what it knows and SAM covers the long tail.

    Returns normalised boxes [{cx,cy,w,h, area, depth_mean, _px}], filtered by
    MIN_BOX_PX, biggest first. Never raises."""
    if _PROPOSAL_SOURCE == "sam":
        try:
            import sam_proposals
            return sam_proposals.propose(img_bgr, depth=depth,
                                         max_regions=max_regions,
                                         seed_boxes=seed_boxes)
        except Exception:
            # Any import/runtime problem with SAM must never break discovery;
            # fall through to the heuristic proposer below.
            pass
    return _propose_regions_heuristic(img_bgr, depth=depth,
                                      max_regions=max_regions,
                                      seed_boxes=seed_boxes)


def _propose_regions_heuristic(img_bgr, depth=None, max_regions=40,
                               seed_boxes=None):
    """Original saliency + contour proposer. When `seed_boxes` (YOLO detections)
    are supplied they are emitted verbatim and heuristic boxes overlapping them
    are dropped, mirroring the SAM path's seeding behaviour."""
    if img_bgr is None or not _HAVE_CV2:
        return []
    try:
        H, W = img_bgr.shape[:2]
        gray = cv2.cvtColor(img_bgr[:, :, :3], cv2.COLOR_BGR2GRAY)

        # edges from luminance + (optionally) depth steps
        edges = cv2.Canny(gray, 50, 150)
        if depth is not None:
            d8 = (np.clip(depth, 0, 1) * 255).astype(np.uint8)
            dedges = cv2.Canny(d8, 30, 90)
            edges = cv2.bitwise_or(edges, dedges)

        # close gaps -> solid blobs
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
        closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, k, iterations=2)
        closed = cv2.dilate(closed, k, iterations=1)

        cnts, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        boxes = []
        img_area = float(H * W)
        for c in cnts:
            x, y, w, h = cv2.boundingRect(c)
            x2, y2 = x + w, y + h
            if not _box_ok(x, y, x2, y2):
                continue
            area = w * h
            # ignore near-whole-image boxes (usually the background blob)
            if area > 0.9 * img_area:
                continue
            dmean = float(depth[y:y2, x:x2].mean()) if depth is not None else 0.0
            boxes.append({"cx": (x + x2) / 2 / W, "cy": (y + y2) / 2 / H,
                          "w": w / W, "h": h / H,
                          "area": area / img_area, "depth_mean": dmean,
                          "_px": (x, y, x2, y2)})

        # YOLO seed boxes: emit verbatim as high-confidence proposals and drop
        # any heuristic box that overlaps a seed (same object). Mirrors the SAM
        # path so "YOLO handles what it knows" holds regardless of source.
        seed_px = []
        if seed_boxes:
            for s in seed_boxes:
                try:
                    sx1 = int(round((s["cx"] - s["w"] / 2) * W))
                    sy1 = int(round((s["cy"] - s["h"] / 2) * H))
                    sx2 = int(round((s["cx"] + s["w"] / 2) * W))
                    sy2 = int(round((s["cy"] + s["h"] / 2) * H))
                except Exception:
                    continue
                sx1, sy1 = max(0, sx1), max(0, sy1)
                sx2, sy2 = min(W, sx2), min(H, sy2)
                if not _box_ok(sx1, sy1, sx2, sy2):
                    continue
                seed_px.append(((sx1, sy1, sx2, sy2),
                                s.get("class_name") or s.get("seed_class")))

            def _iou(a, b):
                ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
                iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
                inter = ix * iy
                if inter <= 0:
                    return 0.0
                ua = ((a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - inter)
                return inter / ua if ua > 0 else 0.0

            if seed_px:
                boxes = [b for b in boxes
                         if all(_iou(b["_px"], sp) <= 0.55 for sp, _c in seed_px)]
                seed_dicts = []
                for (sx1, sy1, sx2, sy2), scls in seed_px:
                    sa = (sx2 - sx1) * (sy2 - sy1)
                    if sa > 0.9 * img_area:
                        continue
                    sd = (float(depth[sy1:sy2, sx1:sx2].mean())
                          if depth is not None else 0.0)
                    d = {"cx": (sx1 + sx2) / 2 / W, "cy": (sy1 + sy2) / 2 / H,
                         "w": (sx2 - sx1) / W, "h": (sy2 - sy1) / H,
                         "area": sa / img_area, "depth_mean": sd,
                         "_px": (sx1, sy1, sx2, sy2)}
                    if scls:
                        # Mark provenance so downstream can tell a known-class
                        # YOLO seed from a discovered proposal.
                        d["seed_class"] = scls
                    seed_dicts.append(d)
                boxes = seed_dicts + boxes

        # biggest first, capped
        boxes.sort(key=lambda b: b["area"], reverse=True)
        return boxes[:max_regions]
    except Exception:
        return []


# ── CNN backbone (optional) ───────────────────────────────────────────────────
def _load_cnn(model_path=None):
    """Lazy-load a timm CNN backbone for embeddings. Returns True on success.
    `model_path` selects the timm architecture (default: efficientnet_b0).
    Re-attempts if a *different* arch is requested than the last try."""
    arch = model_path or "efficientnet_b0"
    if _CNN["loaded"] and _CNN.get("req") == arch:
        return _CNN["model"] is not None
    _CNN["loaded"] = True
    _CNN["req"] = arch
    if not _have_torch():
        _CNN["model"] = None
        return False
    try:
        torch = _get_torch()
        import timm
        model = timm.create_model(arch, pretrained=True, num_classes=0)
        model.eval()
        if torch.cuda.is_available():
            model = model.to("cuda")
        _CNN.update(model=model, path=arch, dim=model.num_features)
        return True
    except Exception:
        _CNN.update(model=None)
        return False


def _cnn_embed(crops_bgr):
    """Embed a list of BGR crops with the CNN backbone -> (N, dim) float32.
    Assumes _load_cnn() already succeeded."""
    torch = _get_torch()
    import torch.nn.functional as F
    xs = []
    for c in crops_bgr:
        rgb = cv2.cvtColor(c[:, :, :3], cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        rgb = cv2.resize(rgb, (224, 224), interpolation=cv2.INTER_AREA)
        xs.append(rgb.transpose(2, 0, 1))
    t = torch.from_numpy(np.stack(xs))
    # ImageNet normalisation
    mean = torch.tensor([0.485, 0.456, 0.406]).view(1, 3, 1, 1)
    std = torch.tensor([0.229, 0.224, 0.225]).view(1, 3, 1, 1)
    t = (t - mean) / std
    if torch.cuda.is_available():
        t = t.to("cuda")
    with torch.no_grad():
        feat = _CNN["model"](t)
        feat = F.normalize(feat, dim=1)
    return feat.detach().cpu().numpy().astype(np.float32)


# ── cv2 fallback embedding (depth + color + shape) ────────────────────────────
def _cv2_embed_one(crop_bgr, depth_crop=None):
    """A compact hand-built descriptor: colour histogram + shape moments +
    depth stats. Length == _EMB_FALLBACK_DIM. L2-normalised."""
    c = cv2.resize(crop_bgr[:, :, :3], (64, 64), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(c, cv2.COLOR_BGR2HSV)
    # colour: small H/S histogram (8x4 = 32)
    hist = cv2.calcHist([hsv], [0, 1], None, [8, 4], [0, 180, 0, 256])
    hist = cv2.normalize(hist, hist).flatten()           # 32
    # shape: Hu moments of the luminance edge map (7)
    g = cv2.cvtColor(c, cv2.COLOR_BGR2GRAY)
    hu = cv2.HuMoments(cv2.moments(cv2.Canny(g, 50, 150))).flatten()
    hu = np.sign(hu) * np.log1p(np.abs(hu))              # 7, log-scaled
    # texture: gradient orientation histogram (16)
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0); gy = cv2.Sobel(g, cv2.CV_32F, 0, 1)
    ang = (np.arctan2(gy, gx) + np.pi) * (180 / np.pi)
    th, _ = np.histogram(ang, bins=16, range=(0, 360),
                         weights=np.hypot(gx, gy))
    th = th / (th.sum() or 1.0)                           # 16
    # depth: mean/std/min/max + near-fraction (5)
    if depth_crop is not None and depth_crop.size:
        d = depth_crop.astype(np.float32)
        dstats = np.array([d.mean(), d.std(), d.min(), d.max(),
                           float((d > 0.6).mean())], np.float32)
    else:
        dstats = np.zeros(5, np.float32)
    vec = np.concatenate([hist, hu, th, dstats]).astype(np.float32)  # 60
    if vec.shape[0] < _EMB_FALLBACK_DIM:
        vec = np.pad(vec, (0, _EMB_FALLBACK_DIM - vec.shape[0]))
    n = np.linalg.norm(vec) or 1.0
    return (vec / n).astype(np.float32)


def embed_regions(img_bgr, boxes, depth=None, cnn_model=None):
    """Embed each proposed box. Uses the CNN backbone when available (depth is
    concatenated as extra channels of stats), else the cv2 descriptor. Returns
    (N, D) float32, rows aligned with `boxes`. Never raises."""
    if not boxes or img_bgr is None or not _HAVE_CV2:
        return np.zeros((0, _EMB_FALLBACK_DIM), np.float32)
    crops, dcrops = [], []
    for b in boxes:
        x1, y1, x2, y2 = b["_px"]
        crops.append(img_bgr[y1:y2, x1:x2])
        dcrops.append(depth[y1:y2, x1:x2] if depth is not None else None)
    try:
        if _load_cnn(cnn_model):
            emb = _cnn_embed(crops)
            # append depth stats so identical-looking objects at different
            # depths can still be told apart when that matters
            if depth is not None:
                extra = np.array([[dc.mean(), dc.std()] if dc is not None and dc.size
                                  else [0, 0] for dc in dcrops], np.float32)
                emb = np.concatenate([emb, extra], axis=1)
            return emb.astype(np.float32)
    except Exception:
        pass
    return np.stack([_cv2_embed_one(c, dc) for c, dc in zip(crops, dcrops)])


# ── grouping ──────────────────────────────────────────────────────────────────
def group_embeddings(embeddings, min_cluster=2, eps=0.18):
    """Cluster embeddings by cosine similarity into groups of similar objects.

    Discovers the number of clusters (no k needed). Scales to 100k+ objects:
      1. HNSW approximate-nearest-neighbour index (hnswlib) — the fast path.
         For each point we pull its neighbours within the cosine radius and
         union-find them into clusters. O(n log n) time and memory; handles the
         high-dimensional CNN embeddings that make a DBSCAN tree degrade to
         brute force.
      2. If hnswlib is unavailable, fall back to DBSCAN on L2-normalised vectors
         with the EUCLIDEAN metric (equivalent to cosine on unit vectors, but
         index-able and O(n^2)-memory-free).
      3. Last resort: a KD-tree greedy union-find.

    `eps` is a COSINE distance threshold (0 = identical, smaller = stricter).
    Returns a label array (N,), -1 == noise/ungrouped. Never raises."""
    n = len(embeddings)
    if n < min_cluster:
        return np.full(n, -1, dtype=int)
    X = np.asarray(embeddings, np.float32)
    norms = np.linalg.norm(X, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    X = X / norms

    hnsw_err = None
    try:
        return _hnsw_group(X, eps, min_cluster)
    except Exception as ex:
        hnsw_err = ex   # remember; only fall back to a SAFE method

    # ── fallbacks, chosen so they can NEVER allocate an O(n^2) matrix ──────────--
    # DBSCAN's brute path builds an n x n distance matrix: at 100k points that's
    # ~80 GB and was the OOM. Only allow DBSCAN when it can use a tree index
    # (low dim) AND n is modest. Otherwise use the KD-tree greedy union-find,
    # which is strictly O(n log n) memory.
    n_small = n <= 50000
    low_dim = X.shape[1] <= 30
    try:
        if n_small and low_dim:
            from sklearn.cluster import DBSCAN
            euc_eps = float(np.sqrt(max(2.0 * eps, 1e-9)))
            return DBSCAN(eps=euc_eps, min_samples=min_cluster, metric="euclidean",
                          algorithm="ball_tree", n_jobs=-1).fit_predict(X).astype(int)
        return _greedy_group(X, eps, min_cluster)
    except Exception:
        return np.full(n, -1, dtype=int)


def _finalise_labels(roots, min_cluster, n):
    """Union-find roots -> contiguous cluster ids, dropping sub-min_cluster
    groups to noise (-1)."""
    from collections import Counter
    cnt = Counter(roots.tolist())
    remap, nxt = {}, 0
    out = np.full(n, -1, dtype=int)
    for i, r in enumerate(roots):
        if cnt[r] < min_cluster:
            continue
        if r not in remap:
            remap[r] = nxt; nxt += 1
        out[i] = remap[r]
    return out


def group_embeddings_streaming(batch_iter, total, dim, eps=0.18, min_cluster=2,
                               ef=100, M=16, k=24, normalise=True,
                               progress=None):
    """Cluster the WHOLE library with bounded RAM by building one HNSW index
    incrementally from streamed batches.

    This exists because the dataset is too big to hold every embedding in a
    Python array at once, but clustering must still be GLOBAL: a person's face in
    image #50 and image #19,000 has to be able to land in the same cluster. A
    per-batch clustering could never do that. So we keep the clustering global
    (ONE index over every object) and make only the *feeding* of vectors
    streaming — each batch is added to the index, then dropped before the next is
    pulled.

    Memory model:
      * Python side holds at most ONE batch of rows at a time.
      * hnswlib keeps its own C++ copy of the vectors + graph — that is the real
        resident floor (~total * dim * 4 bytes for vectors, plus graph). There is
        no way around storing the vectors *somewhere* to do global ANN; this puts
        them in one compact C++ arena instead of millions of Python float objects
        (the old JSON path) or a duplicated numpy copy.
      * The neighbour/union-find pass re-streams the same batches a second time,
        again one batch resident.

    Args:
      batch_iter: a callable returning a FRESH iterator each time it is called
                  (it is called twice — once to add, once to query). Each
                  iteration yields a contiguous float32 ndarray (rows, dim); the
                  rows are assumed to be in a stable global order, with the i-th
                  yielded row mapping to global index i.
      total: total number of object rows that will be yielded (== index size).
      dim: embedding dimension.
      eps: cosine-distance threshold (0 = identical; smaller = stricter).
      min_cluster: groups smaller than this become noise (-1).
      progress: optional callable(done, total, phase) for UI status.

    Returns a label array (total,), -1 == noise/ungrouped. Falls back to a single
    empty result on hard failure. Never raises.
    """
    if total < min_cluster or dim <= 0:
        return np.full(max(total, 0), -1, dtype=int)
    try:
        import hnswlib
    except Exception:
        return np.full(total, -1, dtype=int)

    nt = max(1, os.cpu_count() or 1)
    index = None
    try:
        index = hnswlib.Index(space="cosine", dim=dim)
        index.init_index(max_elements=total, ef_construction=ef, M=M)

        # ── phase 1: ADD every vector, one batch resident at a time ───────────
        added = 0
        for batch in batch_iter():
            if batch is None or len(batch) == 0:
                continue
            b = np.ascontiguousarray(batch, np.float32)
            if normalise:
                nrm = np.linalg.norm(b, axis=1, keepdims=True)
                nrm[nrm == 0] = 1.0
                b = b / nrm
            ids = np.arange(added, added + len(b))
            # hnswlib 'cosine' space normalises internally too, but doing it here
            # keeps the query pass below consistent and cheap.
            index.add_items(b, ids, num_threads=nt)
            added += len(b)
            if progress:
                progress(added, total, "indexing")
            del batch, b, ids
        if added == 0:
            return np.full(total, -1, dtype=int)
        index.set_ef(max(ef // 2, k + 1))

        # ── phase 2: QUERY + union-find, again one batch resident ─────────────
        parent = np.arange(added)
        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]; a = parent[a]
            return a
        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb: parent[rb] = ra

        kk = min(k, added)
        base = 0
        done = 0
        for batch in batch_iter():
            if batch is None or len(batch) == 0:
                continue
            b = np.ascontiguousarray(batch, np.float32)
            if normalise:
                nrm = np.linalg.norm(b, axis=1, keepdims=True)
                nrm[nrm == 0] = 1.0
                b = b / nrm
            labels, dists = index.knn_query(b, k=kk, num_threads=nt)
            for row_i in range(len(b)):
                i = base + row_i
                if i >= added:
                    break
                lr, dr = labels[row_i], dists[row_i]
                for idx_j in range(kk):
                    j = int(lr[idx_j])
                    if j != i and dr[idx_j] <= eps:
                        union(i, j)
            base += len(b)
            done += len(b)
            if progress:
                progress(done, total, "linking")
            del batch, b, labels, dists
        roots = np.fromiter((find(i) for i in range(added)), dtype=int, count=added)
        return _finalise_labels(roots, min_cluster, added)
    except Exception:
        # never crash the app; an empty grouping is a safe degrade
        return np.full(total, -1, dtype=int)
    finally:
        index = None
        try:
            gc.collect()
        except Exception:
            pass


def _hnsw_group(X, eps, min_cluster, ef=100, M=16, k=24):
    """Cluster unit vectors with an HNSW index + union-find. `eps` is cosine
    distance; hnswlib's 'cosine' space returns distance = 1 - cos directly, so we
    threshold on it without conversion. Build and query are multithreaded — the
    index BUILD dominates wall time, so threading it is the main speed lever
    (110k×256d: ~25s threaded vs minutes single-threaded). `k` neighbours per
    point bounds how many same-cluster links we can find."""
    import hnswlib
    n, dim = X.shape
    nt = max(1, os.cpu_count() or 1)
    index = None
    try:
        index = hnswlib.Index(space="cosine", dim=dim)
        index.init_index(max_elements=n, ef_construction=ef, M=M)
        index.add_items(X, np.arange(n), num_threads=nt)
        index.set_ef(max(ef // 2, k + 1))

        parent = np.arange(n)
        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]; a = parent[a]
            return a
        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb: parent[rb] = ra

        kk = min(k, n)
        CH = 8192
        for s in range(0, n, CH):
            e = min(s + CH, n)
            labels, dists = index.knn_query(X[s:e], k=kk, num_threads=nt)
            for row_i in range(e - s):
                i = s + row_i
                lr, dr = labels[row_i], dists[row_i]
                for idx_j in range(kk):
                    j = int(lr[idx_j])
                    if j != i and dr[idx_j] <= eps:
                        union(i, j)
            del labels, dists
        roots = np.fromiter((find(i) for i in range(n)), dtype=int, count=n)
        return _finalise_labels(roots, min_cluster, n)
    finally:
        # hnswlib holds its graph + vectors in C++; drop the Python ref and force
        # a collection so the native memory is released. Without this, repeated
        # runs (every slider tweak / re-run) accumulate indexes until OOM.
        index = None
        gc.collect()


def _greedy_group(X, eps, min_cluster):
    """KD-tree greedy union-find fallback (X already L2-normalised). Used only if
    hnswlib is unavailable. A KD-tree is useless above ~20 dims, so reduce with
    PCA first; this keeps it O(n log n) in time and memory instead of degrading
    to brute force. Strictly bounded — never allocates an n x n matrix."""
    from scipy.spatial import cKDTree
    Xr = X
    if X.shape[1] > 16 and X.shape[0] > 1000:
        try:
            from sklearn.decomposition import PCA
            k = min(16, X.shape[1], X.shape[0] - 1)
            Xr = PCA(n_components=k, svd_solver="randomized",
                     random_state=0).fit_transform(X).astype(np.float32)
            nn = np.linalg.norm(Xr, axis=1, keepdims=True); nn[nn == 0] = 1.0
            Xr = Xr / nn
        except Exception:
            Xr = X
    tree = cKDTree(Xr)
    euc_eps = float(np.sqrt(max(2.0 * eps, 1e-9)))
    parent = np.arange(len(Xr))
    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]; a = parent[a]
        return a
    # batched radius query keeps peak memory flat
    CH = 4096
    for s in range(0, len(Xr), CH):
        e = min(s + CH, len(Xr))
        neigh = tree.query_ball_point(Xr[s:e], euc_eps, workers=-1)
        for off, nb in enumerate(neigh):
            i = s + off
            for j in nb:
                if j != i:
                    ra, rb = find(i), find(j)
                    if ra != rb: parent[rb] = ra
    roots = np.fromiter((find(i) for i in range(len(Xr))), dtype=int, count=len(Xr))
    return _finalise_labels(roots, min_cluster, len(Xr))


def _tag_overlap(tags_a, tags_b):
    sa = {t.lower().strip() for t in (tags_a or [])}
    sb = {t.lower().strip() for t in (tags_b or [])}
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def suggest_cluster_labels(items, labels):
    """Given per-item tags, propose a label for each cluster by majority vote of
    the items' existing tags. `items` is a list of dicts each with a 'tags' list;
    aligned with `labels`. Returns {cluster_id: suggested_label}."""
    from collections import Counter
    by_cluster = {}
    for it, lab in zip(items, labels):
        if lab < 0:
            continue
        by_cluster.setdefault(lab, Counter())
        for t in (it.get("tags") or []):
            by_cluster[lab][t.lower().strip()] += 1
    out = {}
    for lab, cnt in by_cluster.items():
        if cnt:
            out[lab] = cnt.most_common(1)[0][0]
    return out


# ── one-call convenience ──────────────────────────────────────────────────────
def analyse_image(img_bgr, depth_model=None, cnn_model=None, max_regions=40):
    """Full per-image pass: gate size -> depth -> propose -> embed. Returns
    {boxes, embeddings, depth} or None if the image is too small / unusable.
    Boxes keep their pixel coords in '_px' for cropping; callers that persist
    them should drop '_px'."""
    if image_too_small(img_bgr):
        return None
    depth = depth_map(img_bgr, depth_model)
    boxes = propose_regions(img_bgr, depth=depth, max_regions=max_regions)
    if not boxes:
        return {"boxes": [], "embeddings": np.zeros((0, _EMB_FALLBACK_DIM), np.float32),
                "depth": depth}
    emb = embed_regions(img_bgr, boxes, depth=depth, cnn_model=cnn_model)
    return {"boxes": boxes, "embeddings": emb, "depth": depth}


# ── batched cross-image scan (overlap CPU decode with GPU inference) ───────────
def scan_images(loader, names, depth_model=None, cnn_model=None, max_regions=40,
                gpu_batch=8, decode_workers=4, progress=None, should_stop=None):
    """Scan many images with the work pipelined for throughput.

    The serial path (decode -> depth -> propose -> embed, one image at a time)
    leaves the GPU idle during decode/propose and the CPU idle during depth/embed.
    This version runs `decode_workers` CPU threads that decode + (pseudo-)nothing,
    then processes images in GPU batches of `gpu_batch`: ONE depth forward pass
    for the whole batch, proposals on CPU, then ONE embed pass over all crops in
    the batch. That keeps both ends busy and feeds the GPU real batch sizes.

    Args:
      loader: callable(name) -> BGR ndarray or None. Does the decode/IO.
      names:  list of image identifiers passed to loader and returned in results.
      gpu_batch: images per GPU depth batch (crops are batched within too).
      decode_workers: parallel decode threads.
      progress: optional callable(done, total) for status updates.
      should_stop: optional callable() -> bool to cancel mid-scan.

    Yields per-image dicts: {name, boxes, embeddings, skipped, error}. Streaming
    so the caller can accumulate without holding every decoded image in RAM.
    Never raises for a single bad image — that image yields error=True."""
    total = len(names)
    done = [0]
    q = queue.Queue(maxsize=max(8, gpu_batch * 3))   # bounded -> backpressure
    SENTINEL = object()

    def _decode(name):
        try:
            img = loader(name)
            if img is None:
                return (name, None, "error")
            if image_too_small(img):
                return (name, None, "skipped")
            return (name, img, None)
        except Exception:
            return (name, None, "error")

    def _producer():
        with ThreadPoolExecutor(max_workers=decode_workers) as ex:
            for res in ex.map(_decode, names):
                if should_stop and should_stop():
                    break
                q.put(res)
        q.put(SENTINEL)

    threading.Thread(target=_producer, daemon=True).start()

    batch = []   # list of (name, bgr)

    def _flush(batch):
        """Process one batch: batched depth -> per-image propose -> batched embed."""
        if not batch:
            return []
        imgs = [b[1] for b in batch]
        depths = depth_map_batch(imgs, depth_model)
        out = []
        # collect all crops across the batch for ONE embed pass
        all_crops, all_dcrops, owner = [], [], []   # owner[i] = index into batch
        per_boxes = []
        for bi, (name, img) in enumerate(batch):
            d = depths[bi] if bi < len(depths) else None
            boxes = propose_regions(img, depth=d, max_regions=max_regions)
            per_boxes.append(boxes)
            for b in boxes:
                x1, y1, x2, y2 = b["_px"]
                all_crops.append(img[y1:y2, x1:x2])
                all_dcrops.append(d[y1:y2, x1:x2] if d is not None else None)
                owner.append(bi)
        # one embed call for every crop in the batch
        embs = _embed_crops(all_crops, all_dcrops, cnn_model) if all_crops \
            else np.zeros((0, _EMB_FALLBACK_DIM), np.float32)
        # scatter embeddings back to their images
        split = {}
        for k, bi in enumerate(owner):
            split.setdefault(bi, []).append(embs[k])
        for bi, (name, img) in enumerate(batch):
            rows = split.get(bi, [])
            out.append({"name": name, "boxes": per_boxes[bi],
                        "embeddings": np.array(rows, np.float32) if rows
                        else np.zeros((0, embs.shape[1] if embs.size else _EMB_FALLBACK_DIM), np.float32),
                        "skipped": False, "error": False})
        return out

    while True:
        item = q.get()
        if item is SENTINEL:
            break
        name, img, flag = item
        if flag == "skipped":
            done[0] += 1
            if progress: progress(done[0], total)
            yield {"name": name, "boxes": [], "embeddings": None,
                   "skipped": True, "error": False}
            continue
        if flag == "error":
            done[0] += 1
            if progress: progress(done[0], total)
            yield {"name": name, "boxes": [], "embeddings": None,
                   "skipped": False, "error": True}
            continue
        batch.append((name, img))
        if len(batch) >= gpu_batch:
            for r in _flush(batch):
                done[0] += 1
                if progress: progress(done[0], total)
                yield r
            batch = []
        if should_stop and should_stop():
            break
    # final partial batch
    for r in _flush(batch):
        done[0] += 1
        if progress: progress(done[0], total)
        yield r


def _embed_crops(crops_bgr, dcrops, cnn_model=None):
    """Embed a flat list of crops (already cut out) in one pass. Mirrors
    embed_regions but takes crops directly so a whole batch's crops embed
    together. Returns (N, D) float32."""
    if not crops_bgr:
        return np.zeros((0, _EMB_FALLBACK_DIM), np.float32)
    try:
        if _load_cnn(cnn_model):
            emb = _cnn_embed(crops_bgr)
            extra = np.array([[dc.mean(), dc.std()] if dc is not None and dc.size
                              else [0, 0] for dc in dcrops], np.float32)
            return np.concatenate([emb, extra], axis=1).astype(np.float32)
    except Exception:
        pass
    return np.stack([_cv2_embed_one(c, dc) for c, dc in zip(crops_bgr, dcrops)])