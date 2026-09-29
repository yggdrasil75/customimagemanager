"""
Train the duplicate-detector CNN size series from this library (and/or
dataset folders on disk).
======================================================================
Images come from the app's own library and/or extra folders; the build
streams synthetic duplicate / non-duplicate pairs (synth.py) out of them,
mixes in the REAL pairs the user labelled in the Dedup panel (merge =
duplicate, "not a duplicate" = not; the dup_cnn_samples table) and trains
one siamese CNN per selected size (nano..xxl, see dup_cnn.SIZES) on the
SAME stream, so one data pass serves every size. Labels are graded 0..1: a
local edit is labelled by its untouched fraction, so the score means "how
much of the image is the same", not just dup / not. Each size is scored on a
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
pairs per chunk (fresh random augmentations = the augmentation) on a
process pool (`workers` procs, each mmaps the cache), the next chunk
prepared while the GPU trains the current one. Pairs travel as uint8 and
become float on the device. Runs on its own thread;
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
            "epoch": 0, "epochs": 0, "loss": {}, "started": 0.0, "last": None, "error": None,
            "log": [], "history": []}   # log: last 200 status lines; history: [[step, size, loss]...]


def _say(host, msg):
    host.config["status_text"] = "Dedup train: " + msg
    host.logger.info("dedup_train: " + msg)
    progress["log"] = (progress["log"] + [f"{time.strftime('%H:%M:%S')} {msg}"])[-200:]


def _note_loss(z, loss):
    progress["loss"][z] = round(loss, 4)
    progress["history"] = (progress["history"] + [[len(progress["history"]), z, round(loss, 5)]])[-5000:]


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
    """Process-pool worker: decode one file to a side x side uint8 BGR crop
    at NATIVE resolution from a random position (real pixel detail, noise
    and sharpness — what the change net sees at scan time). Images smaller
    than a crop are used whole, upscaled. None when unusable. Standalone on
    purpose: no app state crosses the process boundary."""
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
        h, w = img.shape[:2]
        rng = np.random.default_rng(int(hashlib.sha1(path.encode()).hexdigest()[:8], 16))
        if min(h, w) >= side:
            y, x = int(rng.integers(0, h - side + 1)), int(rng.integers(0, w - side + 1))
            return np.ascontiguousarray(img[y:y + side, x:x + side])
        return cv2.resize(img, (side, side), interpolation=cv2.INTER_LINEAR)
    except Exception:
        return None


def cache_path(host, paths, side):
    key = hashlib.sha1(("\n".join(paths) + f"|{side}|v3").encode()).hexdigest()[:16]
    d = os.path.join(host.core.models_dir, "dedup_train")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"cache_{key}.npy")


def _train_dir(host):
    d = os.path.join(host.core.models_dir, "dedup_train")
    os.makedirs(d, exist_ok=True)
    return d


def clean_train_dir(host, keep=()):
    """Remove everything in models/dedup_train except `keep` (full paths):
    stale caches from other file lists / sides, half-written .part files,
    checkpoints of other builds. Returns bytes freed."""
    d, freed = _train_dir(host), 0
    keep = {os.path.abspath(k) for k in keep}
    for name in os.listdir(d):
        p = os.path.join(d, name)
        if os.path.abspath(p) in keep or not os.path.isfile(p):
            continue
        try:
            freed += os.path.getsize(p); os.remove(p)
        except OSError:
            pass
    return freed


def ckpt_path(host, cache_file):
    return cache_file[:-4] + ".ckpt.pt"


def build_cache(host, paths, side, workers, in_ram=False):
    """Decode every path once into a [N, side, side, 3] uint8 .npy of
    native-resolution crops (skipping files that fail) and return (array,
    kept_paths). Reused on later builds with the same file list and side.
    in_ram loads the whole array instead of memory-mapping it (side²·3 bytes
    per image: 196 KB at 256 → 12.8 GB for 65k images)."""
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
    """pairs -> (a, b, m, kinds): a, b uint8 [n,S,S,3] aligned pairs, m float32
    [n,S/8,S/8] per-cell change target (mean of the per-pixel mask)."""
    a_l, b_l, m_l, k_l = [], [], [], []
    for a, b, m, kind in pairs:
        if a is None or b is None or a.shape != b.shape:
            continue
        a_l.append(a); b_l.append(b); m_l.append(dc.cell_mask(m)); k_l.append(kind)
    if not a_l:
        return None
    return np.stack(a_l), np.stack(b_l), np.stack(m_l).astype(np.float32), k_l


_WCACHE = {}    # pair-gen worker: cache path -> mmap'd array (one per process)


def _pairs_worker(args):
    """Process-pool worker: synthetic pairs for a slice of cache indices.
    Standalone (mmaps the cache itself), so it works under fork or spawn."""
    cache_file, idx, per_image, seed = args
    arr = _WCACHE.get(cache_file)
    if arr is None:
        arr = _WCACHE[cache_file] = np.load(cache_file, mmap_mode="r")
    imgs = [np.ascontiguousarray(arr[i]) for i in idx]
    rng = np.random.default_rng(seed)
    pairs = synth.synth_pairs(imgs, rng, per_image=int(per_image)) if len(imgs) >= 2 else []
    rng.shuffle(pairs)
    return _cnn_arrays(pairs) if pairs else None


def _cat(parts):
    parts = [p for p in parts if p is not None]
    if not parts:
        return None
    return (np.concatenate([p[0] for p in parts]), np.concatenate([p[1] for p in parts]),
            np.concatenate([p[2] for p in parts]), sum((p[3] for p in parts), []))


def _feedback_arrays(fb_cnn):
    """(blob, label) rows from dup_cnn_samples -> (a, b, m) arrays, or None.
    label 1 (merged) -> all-zero change target, 0 -> all-one. Old float CHW
    samples are converted to WORK-side uint8."""
    a_l, b_l, m_l = [], [], []
    cells = dc.WORK // dc.STRIDE

    def _u8(x):
        if x.dtype != np.uint8:                       # old [3,h,w] float 0..1
            x = (np.clip(x, 0, 1) * 255).astype(np.uint8).transpose(1, 2, 0)
        return dc._to_work_u8(x)
    for blob, lab in fb_cnn or ():
        try:
            d = np.load(io.BytesIO(blob))
            a, b = _u8(d["a"]), _u8(d["b"])
            if a is None or b is None:
                continue
            a_l.append(a); b_l.append(b)
            m_l.append(np.full((cells, cells), 0.0 if float(lab) >= 0.5 else 1.0, np.float32))
        except Exception:
            continue
    if not a_l:
        return None
    return np.stack(a_l), np.stack(b_l), np.stack(m_l)


def _batches(arr, batch):
    a, b, y = arr[:3]
    return ((a[i:i + batch], b[i:i + batch], y[i:i + batch]) for i in range(0, len(y), int(batch)))


def _acc(cnn, arr, device, kinds=None):
    """Accuracy (score>=0.5 vs unchanged-fraction>=0.5) of one model on
    prepared (a, b, m) arrays; per kind when given, plus "mae" = mean
    |score - unchanged fraction|, which is what matters for graded edits."""
    a, b, m = arr[:3]
    y = 1.0 - m.mean(axis=(1, 2))
    p = np.concatenate([cnn.predict_batch(a[i:i + 64], b[i:i + 64], device) for i in range(0, len(y), 64)])
    ok = (p >= 0.5) == (y >= 0.5)
    if kinds is None:
        return round(float(ok.mean()), 3)
    kinds = np.asarray(kinds)
    rep = {k: round(float(ok[kinds == k].mean()), 3) for k in sorted(set(kinds))}
    rep["all"] = round(float(ok.mean()), 3)
    rep["mae"] = round(float(np.abs(p - y).mean()), 3)
    return rep


def _evaluate(models, hold_imgs, rng, per_image, devs):
    """Held-out accuracy per pair kind for every size (images never trained on)."""
    imgs = [np.ascontiguousarray(im) for im in hold_imgs]
    if len(imgs) < 4:
        return {}
    arr = _cnn_arrays(synth.synth_pairs(imgs, rng, per_image=per_image))
    if arr is None:
        return {}
    return {name: _acc(m, arr, devs[name], arr[3]) for name, m in models.items() if m.trained}


def devices(spec, sizes):
    """Size -> device. `spec`: "" (auto: first GPU or CPU), "cuda:1" (all sizes),
    "cuda:0,cuda:1" (sizes spread over the list, biggest first on the first
    device), or "xl=cuda:0,nano=cuda:1" (explicit; unmapped sizes use the plain
    devices / auto). Sizes on different devices train concurrently."""
    items = [x.strip() for x in str(spec or "").replace(";", ",").split(",") if x.strip()]
    explicit = {k.strip().lower(): v.strip() for k, v in (x.split("=", 1) for x in items if "=" in x)}
    plain = [x for x in items if "=" not in x] or [("cuda" if dc._HAVE_TORCH and dc.torch.cuda.is_available() else "cpu")]
    order = sorted(sizes, key=lambda z: -dc.count_params(sizes[z]["width"], sizes[z]["depth"]))
    return {z: explicit.get(z, plain[i % len(plain)]) for i, z in enumerate(order)}


def gpus():
    """[{value: "cuda:N", label: name}] for the device picker (empty = CPU only)."""
    if not (dc._HAVE_TORCH and dc.torch.cuda.is_available()):
        return []
    return [{"value": f"cuda:{i}", "label": f"cuda:{i} {dc.torch.cuda.get_device_name(i)}"}
            for i in range(dc.torch.cuda.device_count())]


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
          seed=0, install=True, ship=False, cache_side=CACHE_SIDE, in_ram=False, amp="bf16",
          device="", compile=False, micro=0, resume=True, on_installed=None):
    """Blocking build. `paths`: image files to learn from (library + extra
    folders, already scanned). `feedback`: {"cnn": [(blob, label)]} from the
    Dedup panel's merge / not-a-duplicate decisions. `sizes`: {name: {width,
    depth}} (default dc.SIZES), all trained on the same stream. `active`: which size becomes
    models/dup_cnn.pt (default: the largest trained). `device`: see devices()
    (one GPU, several to spread the sizes over, or size=device). `amp`: "bf16",
    "fp16" or "". `compile`: torch.compile the nets. `micro`: GPU micro-batch
    (gradient accumulation up to `batch`; 0 = whole batch). `resume`: continue
    from the checkpoint of an interrupted build with the same cache and sizes
    (a checkpoint is written after every chunk, at most once a minute, and
    removed when the build finishes). Returns the summary (also
    progress['last'])."""
    if not _lock.acquire(blocking=False):
        return {"ok": False, "error": "a build is already running"}
    if not dc._HAVE_TORCH:
        _lock.release()
        return {"ok": False, "error": "torch is not installed"}
    _stop.clear()
    progress.update(running=True, phase="scanning", images_total=0, images_done=0, pairs=0,
                    epoch=0, epochs=int(epochs), loss={}, started=time.time(), error=None,
                    log=[], history=[])
    sizes = dict(sizes or dc.SIZES)
    active = active if active in sizes else list(sizes)[-1]
    fb_arr = _feedback_arrays((feedback or {}).get("cnn"))
    summary = {"ok": False, "images": 0, "pairs": 0, "sizes": {z: {} for z in sizes}, "active": active,
               "feedback_pairs": 0 if fb_arr is None else int(len(fb_arr[2]))}
    pool = None
    try:
        paths = list(paths)
        if not paths:
            raise RuntimeError("no images to learn from (empty library and no dataset folders)")
        rnd = random.Random(seed)
        rnd.shuffle(paths)
        paths = paths[:int(max_images)]
        progress.update(images_total=len(paths), phase="decoding")
        _say(host, f"decoding {len(paths)} images into the cache...")
        paths_all = paths
        cache_file = cache_path(host, paths_all, int(cache_side))
        freed = clean_train_dir(host, keep=(cache_file, cache_file + ".paths", ckpt_path(host, cache_file)))
        if freed:
            _say(host, f"cleaned models/dedup_train: {freed / 2**30:.1f} GB of stale caches removed")
        cache, paths = build_cache(host, paths, int(cache_side), workers, in_ram=bool(in_ram))
        n_hold = max(8, int(len(paths) * holdout)) if len(paths) >= 40 else 0
        hold_idx, train_idx = list(range(n_hold)), list(range(n_hold, len(paths)))
        summary["images"] = len(train_idx)
        progress.update(images_total=len(train_idx) * int(epochs), images_done=0, phase="training")
        rng = np.random.default_rng(seed)

        # Pair-gen pool (forked before this build's GPU work; workers never
        # touch torch, each mmaps the cache file, so a fork is safe).
        pool = ProcessPoolExecutor(max(1, int(workers)))
        models = {z: dc.DupCNN.sized(z, sizes) for z in sizes}
        devs = devices(device, sizes)
        groups = {}                                   # device -> sizes; one training thread per device
        for z, d in devs.items():
            groups.setdefault(d, []).append(z)
        _say(host, "devices: " + ", ".join(f"{z} on {d}" for z, d in devs.items()) + f"; amp {amp or 'off'}")
        if any(d != "cpu" for d in groups):
            dc.torch.backends.cudnn.benchmark = True
            if compile:
                _say(host, "compiling nets: " + ", ".join(z for z, m in models.items() if devs[z] != "cpu" and m.compile()))
        opts = {z: {} for z in sizes}
        done = 0

        # ── checkpoint / resume ───────────────────────────────────────────
        ck = ckpt_path(host, cache_file)
        start_ep, start_ci, last_ck = 0, 0, [0.0]
        if resume and os.path.exists(ck):
            try:
                st = dc.torch.load(ck, map_location="cpu", weights_only=False)
                if set(st["models"]) == set(models) and st.get("seed") == seed and st.get("chunk") == int(chunk):
                    for z, m in models.items():
                        getattr(m.net, "_orig_mod", m.net).load_state_dict(st["models"][z]["model"])
                        m.trained = True
                        opts[z]["opt_state"] = st["models"][z].get("opt")
                    start_ep, start_ci = int(st["epoch"]), int(st["chunk_index"]) + 1
                    done, summary["pairs"] = int(st["done"]), int(st["pairs"])
                    progress.update(images_done=done, pairs=summary["pairs"])
                    _say(host, f"resuming from checkpoint: epoch {start_ep + 1}, chunk {start_ci}, {done} images done")
                else:
                    _say(host, "checkpoint is for different sizes/seed/chunk; starting over")
            except Exception as e:
                _say(host, f"checkpoint unreadable ({e}); starting over")

        def save_ckpt(ep, ci, force=False):
            if not force and time.time() - last_ck[0] < 60:
                return
            st = {"models": {z: {"model": getattr(m.net, "_orig_mod", m.net).state_dict(),
                                 "opt": opts[z]["opt"].state_dict() if opts[z].get("opt") else None}
                             for z, m in models.items()},
                  "epoch": ep, "chunk_index": ci, "done": done, "pairs": summary["pairs"],
                  "seed": seed, "chunk": int(chunk)}
            dc.torch.save(st, ck + ".tmp"); os.replace(ck + ".tmp", ck)
            last_ck[0] = time.time()

        def make_pairs(idx):
            idx = sorted(idx)
            n = max(1, min(int(workers), len(idx) // 8))          # >= 8 images per shard for "unrelated"
            shards = [idx[i::n] for i in range(n)]
            arr = _cat(pool.map(_pairs_worker, [(cache_file, sh, int(per_image), int(rng.integers(2**31)))
                                                for sh in shards if len(sh) >= 2]))
            return (0 if arr is None else len(arr[2])), arr

        def train_all(arr):
            def run(zs):
                for z in zs:
                    loss = models[z].fit_batches(_batches(arr, batch), lr=lr, device=devs[z],
                                                 _opt_holder=opts[z], amp=amp, micro=int(micro or 0))
                    if loss is not None:
                        _note_loss(z, loss)
            if len(groups) == 1:
                run(next(iter(groups.values())))
            else:                                     # each device trains its sizes at the same time
                with ThreadPoolExecutor(len(groups)) as ex:
                    list(ex.map(run, groups.values()))

        with ThreadPoolExecutor(1) as pre:          # prepares the NEXT chunk while the GPU trains this one
            for ep in range(int(epochs)):
                progress["epoch"] = ep + 1
                order = list(train_idx)
                rnd.shuffle(order)                    # same sequence every run for one seed: resumable
                if ep < start_ep:
                    continue
                chunks = list(_chunks(order, int(chunk)))
                first = start_ci if ep == start_ep else 0
                fut = pre.submit(make_pairs, chunks[first]) if first < len(chunks) else None
                for ci in range(first, len(chunks)):
                    chunk_idx = chunks[ci]
                    if _stop.is_set():
                        save_ckpt(ep, ci - 1, force=True)
                        raise RuntimeError("stopped")
                    n_pairs, arr = fut.result()
                    fut = pre.submit(make_pairs, chunks[ci + 1]) if ci + 1 < len(chunks) else None
                    done += len(chunk_idx)
                    progress.update(images_done=done, pairs=progress["pairs"] + n_pairs)
                    summary["pairs"] += n_pairs
                    if arr is not None:
                        train_all(arr)
                    save_ckpt(ep, ci)
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
                save_ckpt(ep, len(chunks) - 1, force=True)

        progress["phase"] = "evaluating"
        _say(host, f"evaluating {len(sizes)} size(s) on {len(hold_idx)} held-out images...")
        held = _evaluate(models, [cache[i] for i in hold_idx], rng, int(per_image), devs) if hold_idx else {}
        for z, m in models.items():
            row = summary["sizes"][z]
            row.update({"width": m.width_mult, "depth": m.depth, "params": m.params, "device": devs[z],
                        "final_loss": progress["loss"].get(z), "held_out": held.get(z, {}),
                        "feedback": _acc(m, fb_arr, devs[z]) if (fb_arr is not None and m.trained) else None})
            progress["phase"] = f"benchmarking {z}"
            _say(host, f"benchmarking {z}...")
            row["bench_cpu"] = m.bench("cpu", batch=min(int(batch), 64))
            if devs[z] != "cpu":
                row["bench_gpu"] = m.bench(devs[z], batch=int(batch))

        progress["phase"] = "writing"
        _say(host, "writing models...")
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
        if written and os.path.exists(ck):
            os.remove(ck)                                 # finished: the checkpoint has done its job
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
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
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