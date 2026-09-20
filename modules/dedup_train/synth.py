"""
Build shippable pretrained duplicate detectors from dataset dumps on disk.
======================================================================
Not the user's library: point it at folders of images (AVA, booru exports,
anything), it scans them, streams synthetic duplicate / non-duplicate
pairs (synth.py) and fits both dedup scorers, then writes the files the
scorer modules ship and fall back to:

    modules/dedup_heuristic/pretrained/dup_model.json     (logistic)
    modules/dedup_cnn/pretrained/dup_cnn.pt               (siamese CNN)

Memory is bounded by the chunk size: images are decoded in chunks, pairs
are made per chunk and fed to the CNN as minibatches; the logistic model
only needs 9 floats per pair and is fitted once from a capped sample.
Every epoch regenerates pairs (fresh random augmentations) — that is the
augmentation. A held-out slice of IMAGES (never seen in training) reports
accuracy per pair kind for both models so a build can be judged before it
is shipped. Runs on its own thread; stoppable; one build at a time.
"""
import io
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from . import synth
from modules.dedup_heuristic import dup_heuristics as dh
from modules.dedup_cnn import dup_cnn as dc

IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".jxl", ".avif"}
WORK_LONG_SIDE = 512            # decode target; the scorers work at ≤ 256 anyway
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


def _decode(core, path):
    try:
        img = core.read_image(path) if path.lower().endswith((".jxl", ".avif")) else synth.cv2.imread(path)
        img = core.to_bgr(img) if img is not None else None
    except Exception:
        img = None
    if img is None or img.ndim != 3 or min(img.shape[:2]) < 32:
        return None
    h, w = img.shape[:2]
    s = WORK_LONG_SIDE / float(max(h, w))
    if s < 1:
        img = synth.cv2.resize(img, (max(16, int(w * s)), max(16, int(h * s))),
                               interpolation=synth.cv2.INTER_AREA)
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


def _evaluate(core, heur, cnn, hold_paths, rng, per_image, workers, device):
    """Held-out accuracy per pair kind for each model (images never trained on)."""
    with ThreadPoolExecutor(workers) as ex:
        imgs = [im for im in ex.map(lambda p: _decode(core, p), hold_paths) if im is not None]
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


def build(host, folders, max_images=200_000, per_image=6, epochs=3, chunk=256, batch=32,
          width=1.0, lr=1e-3, workers=4, holdout=0.03, seed=0, targets=("heuristic", "cnn"),
          out_heur=OUT_HEUR, out_cnn=OUT_CNN, install=False):
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
        n_hold = max(8, int(len(paths) * holdout)) if len(paths) >= 40 else 0
        hold, train = paths[:n_hold], paths[n_hold:]
        summary["images"] = len(train)
        progress.update(images_total=len(train) * int(epochs), phase="training")
        rng = np.random.default_rng(seed)

        heur = dh.DuplicateClassifier() if "heuristic" in targets else None
        cnn = dc.DupCNN(width) if ("cnn" in targets and dc._HAVE_TORCH) else None
        device = "cuda" if (cnn and dc.torch.cuda.is_available()) else "cpu"
        opt_holder = {}
        hX, hy = [], []
        done = 0
        with ThreadPoolExecutor(int(workers)) as ex:
            for ep in range(int(epochs)):
                progress["epoch"] = ep + 1
                order = list(train)
                rnd.shuffle(order)
                for chunk_paths in _chunks(order, int(chunk)):
                    if _stop.is_set():
                        raise RuntimeError("stopped")
                    imgs = [im for im in ex.map(lambda p: _decode(core, p), chunk_paths) if im is not None]
                    pairs = synth.synth_pairs(imgs, rng, per_image=int(per_image)) if len(imgs) >= 2 else []
                    rng.shuffle(pairs)
                    done += len(chunk_paths)
                    progress.update(images_done=done, pairs=progress["pairs"] + len(pairs))
                    summary["pairs"] += len(pairs)
                    if heur is not None and ep == 0 and len(hX) < HEUR_MAX_PAIRS:
                        for a, b, lab, _ in pairs:
                            f = dh.extract_features(a, b)
                            if f is not None:
                                hX.append(np.asarray(f, np.float32)); hy.append(lab)
                    if cnn is not None and pairs:
                        arr = _cnn_arrays(pairs)
                        if arr is not None:
                            a, b, y = arr
                            loss = cnn.fit_batches(((a[i:i + batch], b[i:i + batch], y[i:i + batch])
                                                    for i in range(0, len(y), int(batch))),
                                                   lr=lr, device=device, _opt_holder=opt_holder)
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
        _say(host, f"evaluating on {len(hold)} held-out images…")
        if hold:
            summary["held_out"] = _evaluate(core, heur, cnn, hold, rng, int(per_image), int(workers), device)

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