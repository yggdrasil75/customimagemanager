"""
Build shippable pretrained duplicate detectors from dataset dumps on disk.
======================================================================
Not the user's library: point it at folders of images (AVA, booru exports,
anything), it scans them, streams synthetic duplicate / non-duplicate
pairs (synth.py) and fits both dedup scorers, then writes the files the
scorer modules ship and fall back to:

    modules/dedup_heuristic/pretrained/dup_model.json     (logistic)
    modules/dedup_cnn/pretrained/dup_cnn.pt               (siamese CNN)

Data path: every image is decoded ONCE into a uint8 cache
(models/dedup_train/cache_<hash>.npy, [N, S, S, 3], S = cache side,
squashed to a square exactly like the CNN's own preprocessing) by a
process pool, so JXL decoding is paid once, in parallel, GIL-free. Epochs
then read the cache — memory-mapped, or held in RAM when it fits — and
regenerate synthetic pairs (fresh random augmentations = the
augmentation) per chunk, with the next chunk's pairs prepared on a
background thread while the GPU trains the current one. The logistic
model only needs 9 floats per pair and is fitted once from a capped
sample. A held-out slice of IMAGES (never seen in training) reports
accuracy per pair kind for both models so a build can be judged before
it is shipped. Runs on its own thread; stoppable; one build at a time.
"""
import hashlib
import io
import os
import random
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

import numpy as np

from . import synth
from modules.dedup_heuristic import dup_heuristics as dh
from modules.dedup_cnn import dup_cnn as dc

IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".jxl", ".avif"}
CACHE_SIDE = 256                # cached square side; the scorers work at ≤ 256 anyway
HEUR_MAX_PAIRS = 400_000        # more than enough for 9 weights

_HERE = os.path.dirname(os.path.abspath(__file__))
OUT_HEUR = os.path.abspath(os.path.join(_HERE, "..", "dedup_heuristic", "pretrained", "dup_model.json"))
OUT_CNN = os.path.abspath(os.path.join(_HERE, "..", "dedup_cnn", "pretrained", "dup_cnn.pt"))

_lock = threading.Lock()
_stop = threading.Event()
progress = {"running": False, "phase": "", "images_total": 0, "images_done": 0, "pairs": 0,
            "epoch": 0, "epochs": 0, "loss": None, "started": 0.0, "last": None, "error": None}


def _say(host, msg):
    host.config["status_text"] = "Dedup train: " + msg


def scan(folders, exts=IMG_EXTS):
    """Every image file under the folders, recursively (sorted for determinism)."""
    out = []
    for root in folders:
        root = os.path.expanduser(str(root).strip())
        if not root or not os.path.isdir(root):
            continue
        for dp, _dn, fns in os.walk(root):
            for fn in fns:
                if os.path.splitext(fn)[1].lower() in exts:
                    out.append(os.path.join(dp, fn))
    out.sort()
    return out


def _decode_worker(args):
    """Process-pool worker: decode one file to a CACHE_SIDE square uint8 BGR
    array (squashed, not letterboxed — dup_cnn._to_work_bgr squashes at
    inference, so training sees the same geometry). None when unusable.
    Standalone on purpose: no app state crosses the process boundary."""
    path, side = args
    import cv2
    import numpy as np
    try:
        low = path.lower()
        if low.endswith((".jxl", ".avif")):
            import imagecodecs
            with open(path, "rb") as f:
                data = f.read()
            img = imagecodecs.jpegxl_decode(data) if low.endswith(".jxl") else imagecodecs.avif_decode(data)
            while img.ndim > 3:
                img = img[0]
            if img.dtype != np.uint8:
                img = (np.clip(img, 0, 1) * 255).astype(np.uint8) if np.issubdtype(img.dtype, np.floating) \
                      else (img >> 8).astype(np.uint8)
            if img.ndim == 2:
                img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
            elif img.shape[2] == 4:
                img = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
            else:
                img = cv2.cvtColor(img[:, :, :3], cv2.COLOR_RGB2BGR)
        else:
            img = cv2.imread(path, cv2.IMREAD_COLOR)
        if img is None or img.ndim != 3 or min(img.shape[:2]) < 32:
            return None
        return cv2.resize(img, (side, side), interpolation=cv2.INTER_AREA)
    except Exception:
        return None


def cache_path(host, paths, side):
    key = hashlib.sha1(("\n".join(paths) + f"|{side}").encode()).hexdigest()[:16]
    d = os.path.join(host.core.models_dir, "dedup_train")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"cache_{key}.npy")


def build_cache(host, paths, side, workers, in_ram=False):
    """Decode every path once into a [N, side, side, 3] uint8 .npy (skipping
    files that fail) and return (array, kept_paths). Reused on later builds
    with the same file list and side. in_ram loads the whole array instead of
    memory-mapping it (~side²·3 bytes per image: 196 KB at 256 → 12.8 GB for
    65k images)."""
    cp = cache_path(host, paths, side)
    meta = cp + ".paths"
    if os.path.exists(cp) and os.path.exists(meta):
        kept = open(meta, encoding="utf-8").read().split("\n")
        arr = np.load(cp, mmap_mode=None if in_ram else "r")
        if len(kept) == arr.shape[0]:
            return arr, kept
    tmp = cp + ".part"
    n = len(paths)
    arr = np.lib.format.open_memmap(tmp, mode="w+", dtype=np.uint8, shape=(n, side, side, 3))
    kept = []
    j = 0
    with ProcessPoolExecutor(max(1, int(workers))) as ex:
        for i, img in enumerate(ex.map(_decode_worker, ((p, side) for p in paths), chunksize=16)):
            if _stop.is_set():
                del arr; os.remove(tmp)
                raise RuntimeError("stopped")
            if img is not None:
                arr[j] = img; kept.append(paths[i]); j += 1
            if i % 500 == 0:
                progress.update(images_done=i)
                _say(host, f"decoding {i}/{n} into cache ({j} ok)…")
    arr.flush(); del arr
    if j < n:                       # drop failed slots: rewrite compact
        src = np.load(tmp, mmap_mode="r")
        out = np.lib.format.open_memmap(cp, mode="w+", dtype=np.uint8, shape=(j, side, side, 3))
        out[:] = src[:j]; out.flush(); del out, src
        os.remove(tmp)
    else:
        os.replace(tmp, cp)
    open(meta, "w", encoding="utf-8").write("\n".join(kept))
    return np.load(cp, mmap_mode=None if in_ram else "r"), kept


def _decode(core, path):
    """Single-file decode (used only when no cache is wanted)."""
    img = _decode_worker((path, CACHE_SIDE))
    return img


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _cnn_arrays(pairs):
    """pairs -> (a, b, y) float32 arrays in the CNN's stored-sample layout."""
    a_l, b_l, y_l = [], [], []
    for a, b, lab, _ in pairs:
        wa, wb = dc._to_work_bgr(a), dc._to_work_bgr(b)
        if wa is None or wb is None:
            continue
        a_l.append(wa); b_l.append(wb); y_l.append(float(lab))
    if not a_l:
        return None
    return np.stack(a_l), np.stack(b_l), np.asarray(y_l, np.float32)


def _evaluate(core, heur, cnn, hold_imgs, rng, per_image, workers, device):
    """Held-out accuracy per pair kind for each model (images never trained on)."""
    imgs = [np.ascontiguousarray(im) for im in hold_imgs]
    if len(imgs) < 4:
        return {}
    pairs = synth.synth_pairs(imgs, rng, per_image=per_image)
    rep = {"pairs": len(pairs), "heuristic": {}, "cnn": {}}
    kinds = sorted(set(k for *_, k in pairs))
    if heur is not None:
        ok = {k: [] for k in kinds}
        for a, b, lab, k in pairs:
            f = dh.extract_features(a, b)
            if f is not None:
                ok[k].append((heur.predict(f) >= 0.5) == bool(lab))
        rep["heuristic"] = {k: round(float(np.mean(v)), 3) for k, v in ok.items() if v}
        rep["heuristic"]["all"] = round(float(np.mean([x for v in ok.values() for x in v])), 3)
    if cnn is not None and cnn.available and cnn.trained:
        ok = {k: [] for k in kinds}
        for chunk in _chunks(pairs, 64):
            arr = _cnn_arrays(chunk)
            if arr is None:
                continue
            p = cnn.predict_batch(arr[0], arr[1], device)
            j = 0
            for a, b, lab, k in chunk:
                if dc._to_work_bgr(a) is None or dc._to_work_bgr(b) is None:
                    continue
                ok[k].append((p[j] >= 0.5) == bool(lab)); j += 1
        rep["cnn"] = {k: round(float(np.mean(v)), 3) for k, v in ok.items() if v}
        rep["cnn"]["all"] = round(float(np.mean([x for v in ok.values() for x in v])), 3)
    return rep


def build(host, folders, max_images=200_000, per_image=6, epochs=3, chunk=1024, batch=256,
          width=1.0, lr=1e-3, workers=4, holdout=0.03, seed=0, targets=("heuristic", "cnn"),
          out_heur=OUT_HEUR, out_cnn=OUT_CNN, install=False, cache_side=CACHE_SIDE, in_ram=False,
          amp=True):
    """Blocking build. Returns the summary (also progress['last'])."""
    if not _lock.acquire(blocking=False):
        return {"ok": False, "error": "a build is already running"}
    _stop.clear()
    progress.update(running=True, phase="scanning", images_total=0, images_done=0, pairs=0,
                    epoch=0, epochs=int(epochs), loss=None, started=time.time(), error=None)
    summary = {"ok": False, "folders": list(folders), "images": 0, "pairs": 0,
               "heuristic": None, "cnn": None, "held_out": {}}
    core = host.core
    try:
        _say(host, "scanning folders…")
        paths = scan(folders)
        if not paths:
            raise RuntimeError("no images found under " + ", ".join(map(str, folders)))
        rnd = random.Random(seed)
        rnd.shuffle(paths)
        paths = paths[:int(max_images)]
        progress.update(images_total=len(paths), phase="decoding")
        _say(host, f"decoding {len(paths)} images into the cache…")
        cache, paths = build_cache(host, paths, int(cache_side), workers, in_ram=bool(in_ram))
        n_hold = max(8, int(len(paths) * holdout)) if len(paths) >= 40 else 0
        hold_idx, train_idx = list(range(n_hold)), list(range(n_hold, len(paths)))
        summary["images"] = len(train_idx)
        progress.update(images_total=len(train_idx) * int(epochs), images_done=0, phase="training")
        rng = np.random.default_rng(seed)

        heur = dh.DuplicateClassifier() if "heuristic" in targets else None
        cnn = dc.DupCNN(width) if ("cnn" in targets and dc._HAVE_TORCH) else None
        device = "cuda" if (cnn and dc.torch.cuda.is_available()) else "cpu"
        if device == "cuda":
            dc.torch.backends.cudnn.benchmark = True
        opt_holder = {}
        hX, hy = [], []
        done = 0

        def make_pairs(idx):
            """Decode-free: pairs straight from the cache (sorted reads keep a
            memmap sequential)."""
            imgs = [np.ascontiguousarray(cache[i]) for i in sorted(idx)]
            pairs = synth.synth_pairs(imgs, rng, per_image=int(per_image)) if len(imgs) >= 2 else []
            rng.shuffle(pairs)
            arr = _cnn_arrays(pairs) if cnn is not None and pairs else None
            return pairs, arr

        with ThreadPoolExecutor(1) as pre:          # prepares the NEXT chunk while the GPU trains this one
            for ep in range(int(epochs)):
                progress["epoch"] = ep + 1
                order = list(train_idx)
                rnd.shuffle(order)
                chunks = list(_chunks(order, int(chunk)))
                fut = pre.submit(make_pairs, chunks[0]) if chunks else None
                for ci, chunk_idx in enumerate(chunks):
                    if _stop.is_set():
                        raise RuntimeError("stopped")
                    pairs, arr = fut.result()
                    fut = pre.submit(make_pairs, chunks[ci + 1]) if ci + 1 < len(chunks) else None
                    done += len(chunk_idx)
                    progress.update(images_done=done, pairs=progress["pairs"] + len(pairs))
                    summary["pairs"] += len(pairs)
                    if heur is not None and ep == 0 and len(hX) < HEUR_MAX_PAIRS:
                        for a, b, lab, _ in pairs:
                            f = dh.extract_features(a, b)
                            if f is not None:
                                hX.append(np.asarray(f, np.float32)); hy.append(lab)
                    if arr is not None:
                        a, b, y = arr
                        loss = cnn.fit_batches(((a[i:i + batch], b[i:i + batch], y[i:i + batch])
                                                for i in range(0, len(y), int(batch))),
                                               lr=lr, device=device, _opt_holder=opt_holder, amp=bool(amp))
                        progress["loss"] = None if loss is None else round(loss, 4)
                    eta = ""
                    if done and progress["images_total"]:
                        rate = done / max(1e-6, time.time() - progress["started"])
                        eta = f", ~{int((progress['images_total'] - done) / max(rate, 1e-6) / 60)} min left"
                    _say(host, f"epoch {ep + 1}/{epochs}, {done}/{progress['images_total']} images, "
                               f"{summary['pairs']} pairs" + (f", loss {progress['loss']}" if progress["loss"] is not None else "") + eta)
                if heur is not None and ep == 0 and hX:
                    progress["phase"] = "fitting heuristic"
                    ok = heur.pretrain(np.asarray(hX, np.float64), np.asarray(hy, np.float64))
                    heur.source = "shipped"
                    summary["heuristic"] = {"ok": bool(ok), "samples": len(hX)}
                    progress["phase"] = "training"

        if cnn is not None:
            summary["cnn"] = {"ok": bool(cnn.trained), "width": width, "device": device,
                              "final_loss": progress["loss"]}

        progress["phase"] = "evaluating"
        _say(host, f"evaluating on {len(hold_idx)} held-out images…")
        if hold_idx:
            summary["held_out"] = _evaluate(core, heur, cnn, [cache[i] for i in hold_idx], rng,
                                            int(per_image), int(workers), device)

        progress["phase"] = "writing"
        written = []
        if heur is not None and (summary["heuristic"] or {}).get("ok"):
            os.makedirs(os.path.dirname(out_heur), exist_ok=True)
            if heur.save(out_heur):
                written.append(out_heur)
            if install:
                os.makedirs(host.core.models_dir, exist_ok=True)
                p = os.path.join(host.core.models_dir, "dup_model.json")
                heur.source = "pretrained"
                if heur.save(p):
                    written.append(p)
        if cnn is not None and cnn.trained:
            os.makedirs(os.path.dirname(out_cnn), exist_ok=True)
            cnn.net.to("cpu")
            if cnn.save(out_cnn):
                written.append(out_cnn)
            if install:
                os.makedirs(host.core.models_dir, exist_ok=True)
                p = os.path.join(host.core.models_dir, "dup_cnn.pt")
                if cnn.save(p):
                    written.append(p)
        summary["written"] = written
        summary["ok"] = bool(written)
        summary["seconds"] = round(time.time() - progress["started"])
        ho = summary["held_out"]
        _say(host, f"done in {summary['seconds']} s — {summary['pairs']} pairs from {summary['images']} images; "
                   f"held-out heuristic {ho.get('heuristic', {}).get('all', '–')}, CNN {ho.get('cnn', {}).get('all', '–')}; "
                   f"wrote {len(written)} file(s)" + (" (restart to load the installed copies)" if install else ""))
        return summary
    except Exception as e:
        summary["error"] = str(e)
        progress["error"] = str(e)
        _say(host, "stopped" if str(e) == "stopped" else f"failed: {e}")
        if str(e) != "stopped":
            host.logger.error(f"dedup_train: {e}")
        return summary
    finally:
        progress.update(running=False, phase="idle", last=summary)
        _lock.release()


def start(host, **kw):
    if progress["running"]:
        return False
    threading.Thread(target=build, args=(host,), kwargs=kw, daemon=True, name="dedup-train").start()
    return True


def stop():
    _stop.set()
    return progress["running"]