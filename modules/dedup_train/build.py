"""
Train the duplicate-detector CNN size series from this library (and/or
dataset folders on disk).
======================================================================
Images come from the app's own library and/or extra folders; the build
streams synthetic duplicate / non-duplicate pairs (synth.py) out of them,
mixes in the REAL pairs the user labelled in the Dedup panel (merge =
duplicate, "not a duplicate" = not; the dup_cnn_samples table) and trains
one siamese CNN per selected size (nano..xxl, see dup_cnn.SIZES) on the
SAME stream, so one data pass serves every size. Each size is scored on a
held-out image slice and on the user's own feedback pairs, then benchmarked
(params, ms per pair at batch 1 for a CPU / Pi, batched on the GPU, and
training memory), giving a speed-vs-parameters-vs-accuracy table to pick
sizes from.

Outputs (install, default): models/dup_cnn_<size>.pt for every size, and
models/dup_cnn.pt = the "active" size, which the running scorer reloads at
once. "Ship" also writes modules/dedup_cnn/pretrained/dup_cnn_<size>.pt
(+ dup_cnn.pt = active), ready to commit.

Data path: every image is decoded ONCE into a uint8 cache
(models/dedup_train/cache_<hash>.npy, [N, S, S, 3]) by a process pool.
Epochs read the cache (memory-mapped, or in RAM) and regenerate synthetic
pairs per chunk (fresh random augmentations = the augmentation) with the
next chunk prepared on a background thread. Runs on its own thread;
stoppable; one build at a time.

The 9-float logistic heuristic is no longer trained here; dedup_heuristic
stays as the no-torch fallback with its shipped weights.
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
from modules.dedup_cnn import dup_cnn as dc

IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".jxl", ".avif"}
CACHE_SIDE = 256                # cached square side; the scorers work at <= 256 anyway

_HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.abspath(os.path.join(_HERE, "..", "dedup_cnn", "pretrained"))

_lock = threading.Lock()
_stop = threading.Event()
progress = {"running": False, "phase": "", "images_total": 0, "images_done": 0, "pairs": 0,
            "epoch": 0, "epochs": 0, "loss": {}, "started": 0.0, "last": None, "error": None}


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
                _say(host, f"decoding {i}/{n} into cache ({j} ok)...")
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


def _feedback_arrays(fb_cnn):
    """(blob, label) rows from dup_cnn_samples -> (a, b, y) arrays, or None."""
    a_l, b_l, y_l = [], [], []
    for blob, lab in fb_cnn or ():
        try:
            d = np.load(io.BytesIO(blob))
            a_l.append(d["a"]); b_l.append(d["b"]); y_l.append(float(lab))
        except Exception:
            continue
    if not a_l:
        return None
    return np.stack(a_l), np.stack(b_l), np.asarray(y_l, np.float32)


def _batches(arr, batch):
    a, b, y = arr
    return ((a[i:i + batch], b[i:i + batch], y[i:i + batch]) for i in range(0, len(y), int(batch)))


def _acc(cnn, arr, device, kinds=None):
    """Accuracy of one model on prepared (a, b, y) arrays; per kind when given."""
    a, b, y = arr
    p = np.concatenate([cnn.predict_batch(a[i:i + 64], b[i:i + 64], device) for i in range(0, len(y), 64)])
    ok = (p >= 0.5) == (y >= 0.5)
    if kinds is None:
        return round(float(ok.mean()), 3)
    kinds = np.asarray(kinds)
    rep = {k: round(float(ok[kinds == k].mean()), 3) for k in sorted(set(kinds))}
    rep["all"] = round(float(ok.mean()), 3)
    return rep


def _evaluate(models, hold_imgs, rng, per_image, device):
    """Held-out accuracy per pair kind for every size (images never trained on)."""
    imgs = [np.ascontiguousarray(im) for im in hold_imgs]
    if len(imgs) < 4:
        return {}
    pairs = synth.synth_pairs(imgs, rng, per_image=per_image)
    arr = _cnn_arrays(pairs)
    if arr is None:
        return {}
    kinds = [k for a, b, _l, k in pairs if dc._to_work_bgr(a) is not None and dc._to_work_bgr(b) is not None]
    return {name: _acc(m, arr, device, kinds) for name, m in models.items() if m.trained}


def bench(sizes=None, batch=256):
    """Untrained speed-vs-parameters table for a size table {name: {width, depth}}
    (no data needed)."""
    sizes = sizes or dc.SIZES
    if not dc._HAVE_TORCH:
        return {n: {"params": dc.count_params(v["width"], v["depth"]), "width": v["width"], "depth": v["depth"]}
                for n, v in sizes.items()}
    out = {}
    for name in sizes:
        m = dc.DupCNN.sized(name, sizes)
        row = m.bench("cpu", batch=min(int(batch), 64))
        if dc.torch.cuda.is_available():
            g = m.bench("cuda", batch=int(batch))
            row.update({"gpu_ms_per_pair_batch": g.get("ms_per_pair_batch"),
                        "gpu_train_mem_mb": g.get("train_mem_mb")})
        row["width"], row["depth"] = m.width_mult, m.depth
        out[name] = row
    return out


def build(host, paths, feedback=None, sizes=None, active=None, max_images=200_000,
          per_image=6, epochs=3, chunk=1024, batch=256, lr=1e-3, workers=4, holdout=0.03,
          seed=0, install=True, ship=False, cache_side=CACHE_SIDE, in_ram=False, amp=True,
          on_installed=None):
    """Blocking build. `paths`: image files to learn from (library + extra
    folders, already scanned). `feedback`: {"cnn": [(blob, label)]} from the
    Dedup panel's merge / not-a-duplicate decisions. `sizes`: {name: {width,
    depth}} (default dc.SIZES), all trained on the same stream. `active`: which size becomes
    models/dup_cnn.pt (default: the largest trained). Returns the summary
    (also progress['last'])."""
    if not _lock.acquire(blocking=False):
        return {"ok": False, "error": "a build is already running"}
    if not dc._HAVE_TORCH:
        _lock.release()
        return {"ok": False, "error": "torch is not installed"}
    _stop.clear()
    progress.update(running=True, phase="scanning", images_total=0, images_done=0, pairs=0,
                    epoch=0, epochs=int(epochs), loss={}, started=time.time(), error=None)
    sizes = dict(sizes or dc.SIZES)
    active = active if active in sizes else list(sizes)[-1]
    fb_arr = _feedback_arrays((feedback or {}).get("cnn"))
    summary = {"ok": False, "images": 0, "pairs": 0, "sizes": {z: {} for z in sizes}, "active": active,
               "feedback_pairs": 0 if fb_arr is None else int(len(fb_arr[2]))}
    try:
        paths = list(paths)
        if not paths:
            raise RuntimeError("no images to learn from (empty library and no dataset folders)")
        rnd = random.Random(seed)
        rnd.shuffle(paths)
        paths = paths[:int(max_images)]
        progress.update(images_total=len(paths), phase="decoding")
        _say(host, f"decoding {len(paths)} images into the cache...")
        cache, paths = build_cache(host, paths, int(cache_side), workers, in_ram=bool(in_ram))
        n_hold = max(8, int(len(paths) * holdout)) if len(paths) >= 40 else 0
        hold_idx, train_idx = list(range(n_hold)), list(range(n_hold, len(paths)))
        summary["images"] = len(train_idx)
        progress.update(images_total=len(train_idx) * int(epochs), images_done=0, phase="training")
        rng = np.random.default_rng(seed)

        models = {z: dc.DupCNN.sized(z, sizes) for z in sizes}
        device = "cuda" if dc.torch.cuda.is_available() else "cpu"
        if device == "cuda":
            dc.torch.backends.cudnn.benchmark = True
        opts = {z: {} for z in sizes}
        done = 0

        def make_pairs(idx):
            imgs = [np.ascontiguousarray(cache[i]) for i in sorted(idx)]
            pairs = synth.synth_pairs(imgs, rng, per_image=int(per_image)) if len(imgs) >= 2 else []
            rng.shuffle(pairs)
            return len(pairs), (_cnn_arrays(pairs) if pairs else None)

        def train_all(arr):
            for z, m in models.items():
                loss = m.fit_batches(_batches(arr, batch), lr=lr, device=device,
                                     _opt_holder=opts[z], amp=bool(amp))
                if loss is not None:
                    progress["loss"][z] = round(loss, 4)

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
                    n_pairs, arr = fut.result()
                    fut = pre.submit(make_pairs, chunks[ci + 1]) if ci + 1 < len(chunks) else None
                    done += len(chunk_idx)
                    progress.update(images_done=done, pairs=progress["pairs"] + n_pairs)
                    summary["pairs"] += n_pairs
                    if arr is not None:
                        train_all(arr)
                    eta = ""
                    if done and progress["images_total"]:
                        rate = done / max(1e-6, time.time() - progress["started"])
                        eta = f", ~{int((progress['images_total'] - done) / max(rate, 1e-6) / 60)} min left"
                    losses = " ".join(f"{z} {v}" for z, v in progress["loss"].items())
                    _say(host, f"epoch {ep + 1}/{epochs}, {done}/{progress['images_total']} images, "
                               f"{summary['pairs']} pairs; loss {losses}{eta}")
                if fb_arr is not None:
                    # One pass over the user's real labelled pairs per epoch, after
                    # the synthetic ones, so the library's own dupes get the last word.
                    train_all(fb_arr)

        progress["phase"] = "evaluating"
        _say(host, f"evaluating {len(sizes)} size(s) on {len(hold_idx)} held-out images...")
        held = _evaluate(models, [cache[i] for i in hold_idx], rng, int(per_image), device) if hold_idx else {}
        for z, m in models.items():
            row = summary["sizes"][z]
            row.update({"width": m.width_mult, "depth": m.depth, "params": m.params,
                        "final_loss": progress["loss"].get(z), "held_out": held.get(z, {}),
                        "feedback": _acc(m, fb_arr, device) if (fb_arr is not None and m.trained) else None})
            progress["phase"] = f"benchmarking {z}"
            row["bench_cpu"] = m.bench("cpu", batch=min(int(batch), 64))
            if device == "cuda":
                row["bench_gpu"] = m.bench("cuda", batch=int(batch))

        progress["phase"] = "writing"
        written = []
        for m in models.values():
            m.net.to("cpu")
        for do, base in ((install, host.core.models_dir), (ship, OUT_DIR)):
            if not do:
                continue
            os.makedirs(base, exist_ok=True)
            for z, m in models.items():
                if not m.trained:
                    continue
                p = os.path.join(base, f"dup_cnn_{z}.pt")
                if m.save(p):
                    written.append(p)
                if z == active and m.save(os.path.join(base, "dup_cnn.pt")):
                    written.append(os.path.join(base, "dup_cnn.pt"))
        summary["written"] = written
        summary["ok"] = bool(written)
        summary["installed"] = bool(install and written and on_installed and on_installed(active))
        summary["seconds"] = round(time.time() - progress["started"])
        accs = " ".join(f"{z} {r['held_out'].get('all', '-')}/{r['feedback'] if r['feedback'] is not None else '-'}"
                        for z, r in summary["sizes"].items())
        _say(host, f"done in {summary['seconds']} s: {summary['pairs']} pairs from {summary['images']} images; "
                   f"held-out/feedback accuracy {accs}; wrote {len(written)} file(s), active {active}"
                   + (" (live scorer reloaded)" if summary["installed"] else ""))
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