"""
Pretrain the Personal IQA scorer size series from labelled datasets on disk.
======================================================================
Datasets are folders of images plus a labels file (AVA.txt vote histograms,
or any CSV/TSV of "filename,score"). Each image goes through the same
feature pipeline the personal scorer uses live (encoder embed + tiles,
faces, pose, base IQA, tags), cached in personal_iqa_cache under
"ext:<path>" so a rerun only pays for new images. Images missing any
REQUIRED part (personal_iqa_required, default embed+iqa+face+pose) are
skipped and counted per reason: the scorer is meant to learn proportions,
expression and view together, not just sharpness. Optionally the library's
own star ratings are mixed in. Every selected size (d x depth, see the
personal_iqa_sizes setting) is fitted on the same samples, scored on a
hash-split hold-out (MSE + Spearman, against the base IQA) and benchmarked
(params, ms per image on CPU / GPU). Outputs: models/personal_iqa/iqa/
scorer_<size>.pt per size and scorer.pt = the active size, which the
personal provider reloads at once; the personal Retrain then fine-tunes
from it (it never shrinks a larger pretrained model).
"""
import csv
import os
import threading
import time
import zlib

import numpy as np

IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".jxl", ".avif"}
VAL_PCT = 10

_lock = threading.Lock()
_stop = threading.Event()
progress = {"running": False, "phase": "", "images_total": 0, "images_done": 0, "epoch": 0, "epochs": 0,
            "loss": {}, "started": 0.0, "last": None, "error": None}


def _say(host, msg):
    host.config["status_text"] = "IQA train: " + msg


def parse_dataset_lines(text):
    """"<folder> <labels file>" per line (labels optional when the folder holds
    labels.csv / AVA.txt). Returns [(folder, labels_path)]."""
    out = []
    for line in str(text or "").splitlines():
        parts = [p for p in line.replace("|", " ").split() if p]
        if not parts or parts[0].startswith("#"):
            continue
        folder = os.path.expanduser(parts[0])
        labels = os.path.expanduser(parts[1]) if len(parts) > 1 else None
        if labels is None:
            for cand in ("labels.csv", "labels.txt", "AVA.txt", "ava.txt"):
                if os.path.exists(os.path.join(folder, cand)):
                    labels = os.path.join(folder, cand); break
        out.append((folder, labels))
    return out


def _index_images(folder):
    """basename-without-extension -> path, and basename -> path, for label lookup."""
    idx = {}
    for dp, _dn, fns in os.walk(folder):
        for fn in fns:
            if os.path.splitext(fn)[1].lower() in IMG_EXTS:
                p = os.path.join(dp, fn)
                idx.setdefault(fn, p)
                idx.setdefault(os.path.splitext(fn)[0], p)
    return idx


def read_labels(folder, labels_path):
    """(path, score 0..1) pairs. AVA.txt rows are "idx image_id v1..v10 ..." (mean of
    the 1..10 histogram / 10); anything else is "name,score" CSV/TSV with an
    optional header. Scores > 1 are taken as 1..10 (/10), > 10 as /100."""
    if not labels_path or not os.path.exists(labels_path):
        return []
    idx = _index_images(folder)
    rows = []
    with open(labels_path, encoding="utf-8", errors="replace") as f:
        sample = f.read(4096); f.seek(0)
        ava = all(len(l.split()) >= 12 and l.split()[2].isdigit() for l in sample.splitlines()[:5] if l.strip())
        if ava:
            for l in f:
                p = l.split()
                if len(p) < 12:
                    continue
                votes = np.array([float(x) for x in p[2:12]])
                if votes.sum() <= 0:
                    continue
                rows.append((p[1], float((votes * np.arange(1, 11)).sum() / votes.sum())))
        else:
            for p in csv.reader(f, delimiter="\t" if "\t" in sample else ","):
                if len(p) < 2:
                    continue
                try:
                    rows.append((p[0].strip(), float(p[1])))
                except ValueError:
                    continue                      # header
    if not rows:
        return []
    mx = max(s for _, s in rows)
    div = 100.0 if mx > 10 else 10.0 if mx > 1 else 1.0    # ponytail: scale guessed from the max
    out = []
    for name, s in rows:
        p = idx.get(name) or idx.get(os.path.basename(name)) or idx.get(os.path.splitext(os.path.basename(name))[0])
        if p:
            out.append((p, min(1.0, max(0.0, s / div))))
    return out


def _is_val(key):
    return zlib.crc32(key.encode()) % 100 < VAL_PCT


def bench(svc, sizes, embed_dim=512, batch=64):
    """Untrained speed-vs-parameters per size (params without torch; timings with)."""
    dims = {"embed": embed_dim, "tile": embed_dim, "face": 215, "pose17": 34, "pose133": 266, "iqa": 1}
    out = {}
    for name, sp in sizes.items():
        row = {"d": sp["d"], "depth": sp["depth"], "params": svc["count_params"](dims, sp["d"], sp["depth"])}
        try:
            import torch, model_registry
            sample = {"embed": [[0.1] * embed_dim], "tile": [[0.1] * embed_dim] * 9, "face": [[0.0] * 215],
                      "pose17": [], "pose133": [], "iqa": [[0.5]], "tags": [1, 2, 3] + [0] * 29}
            for dev, key in (("cpu", "ms_per_image_cpu"), (model_registry.device(), "ms_per_image_gpu")):
                if dev == "cpu" and key == "ms_per_image_gpu":
                    continue
                m = svc["Scorer"](dims, sp["d"], sp["depth"]).to(dev).eval()
                n = 1 if dev == "cpu" else batch
                f, mk, t = svc["batch"]([sample] * n, dims, dev)
                with torch.no_grad():
                    m(f, mk, t)
                    if dev == "cuda":
                        torch.cuda.synchronize()
                    t0 = time.perf_counter()
                    for _ in range(5):
                        m(f, mk, t)
                    if dev == "cuda":
                        torch.cuda.synchronize()
                row[key] = round((time.perf_counter() - t0) / 5 / n * 1000, 3)
        except Exception:
            pass
        out[name] = row
    return out


def build(host, datasets, sizes, active=None, use_ratings=False, max_images=100_000, epochs=10,
          batch=64, lr=1e-3, holdout=VAL_PCT, install=True, on_installed=None):
    """Blocking. datasets: [(folder, labels_path)]; sizes: {name: {d, depth}}."""
    if not _lock.acquire(blocking=False):
        return {"ok": False, "error": "a build is already running"}
    svc = host.get_service("personal_iqa")
    if not svc:
        _lock.release()
        return {"ok": False, "error": "personal_iqa module (and torch) required"}
    _stop.clear()
    global VAL_PCT
    VAL_PCT = int(holdout)
    progress.update(running=True, phase="reading labels", images_total=0, images_done=0, epoch=0,
                    epochs=int(epochs), loss={}, started=time.time(), error=None)
    sizes = dict(sizes)
    active = active if active in sizes else list(sizes)[-1]
    summary = {"ok": False, "images": 0, "sizes": {z: {} for z in sizes}, "active": active, "datasets": {},
               "skipped": {}, "required": list(svc["required"]())}
    det = svc["detectors"]()
    off = [r for r in summary["required"] if r in det and not det[r]]
    if off:
        _lock.release()
        return {"ok": False, "error": f"no provider for required data: {', '.join(off)} "
                                      "(enable a face / pose / embed / IQA model first)"}
    db = host.db()
    try:
        labelled = []
        for folder, labels in datasets:
            rows = read_labels(folder, labels)
            summary["datasets"][folder] = len(rows)
            labelled += [("ext:" + p, y) for p, y in rows]
        if use_ratings:
            for r in db.execute("SELECT rel_path, user_stars FROM ratings WHERE user_stars IS NOT NULL").fetchall():
                labelled.append((r["rel_path"], r["user_stars"] / 5.0))
        if not labelled:
            raise RuntimeError("no labelled images (check the dataset lines and labels files)")
        labelled = labelled[:int(max_images)]
        progress.update(images_total=len(labelled), phase="features")
        train, val = [], []
        for i, (key, y) in enumerate(labelled):
            if _stop.is_set():
                raise RuntimeError("stopped")
            try:
                if key.startswith("ext:"):
                    mtime = os.stat(key[4:]).st_mtime
                else:
                    r = db.execute("SELECT mtime FROM files WHERE rel_path=?", (key,)).fetchone()
                    mtime = r["mtime"] if r else None
                fe = svc["features"](db, key, mtime)
            except Exception as e:
                host.logger.warning(f"iqa_train features {key}: {e}"); continue
            if fe.get("_missing"):
                for m in fe["_missing"]:
                    summary["skipped"][m] = summary["skipped"].get(m, 0) + 1
                continue
            (val if _is_val(key) else train).append({"feats": fe, "y": y})
            if i % 25 == 0:
                db.commit(); progress["images_done"] = i
                _say(host, f"features {i}/{len(labelled)}")
        db.commit()
        progress["images_done"] = len(labelled)
        summary["images"] = len(train)
        if not train:
            raise RuntimeError(f"no complete images; skipped for missing {summary['skipped']}")

        progress["phase"] = "training"
        models = {}
        for z, sp in sizes.items():
            progress["loss"][z] = None
            def say(ep, loss, z=z):
                progress["epoch"] = ep; progress["loss"][z] = round(loss, 4)
                _say(host, f"{z} epoch {ep}/{epochs} train mse {loss:.4f}")
            m, metrics = svc["fit"](train, val, sp["d"], sp["depth"], epochs=int(epochs), batch=int(batch),
                                    lr=float(lr), say=say, stop=_stop)
            models[z] = m
            row = summary["sizes"][z]
            row.update(metrics)
            row["params"] = sum(p.numel() for p in m.parameters())
            row["final_loss"] = progress["loss"][z]

        progress["phase"] = "benchmarking"
        embed_dim = len(train[0]["feats"]["embed"][0]) if train[0]["feats"]["embed"] else 1
        for z, b in bench(svc, sizes, embed_dim, batch=int(batch)).items():
            summary["sizes"][z].update({k: v for k, v in b.items() if k.startswith("ms_")})

        progress["phase"] = "writing"
        written = []
        if install:
            for z, m in models.items():
                p = os.path.join(svc["ckpt_dir"], f"scorer_{z}.pt")
                svc["save"](m, summary["sizes"][z], p); written.append(p)
                if z == active:
                    svc["save"](m, summary["sizes"][z]); written.append(svc["ckpt_path"])
        summary["written"] = written
        summary["ok"] = bool(written) or not install
        summary["installed"] = bool(install and written and on_installed and on_installed(active))
        summary["seconds"] = round(time.time() - progress["started"])
        accs = " ".join(f"{z} {r.get('val_spearman', 0):.3f}" for z, r in summary["sizes"].items())
        _say(host, f"done in {summary['seconds']} s: {summary['images']} images (skipped {summary['skipped']}); "
                   f"val spearman {accs}"
                   f" (base {next(iter(summary['sizes'].values())).get('base_spearman', 0):.3f}); active {active}")
        return summary
    except Exception as e:
        summary["error"] = str(e)
        progress["error"] = str(e)
        _say(host, "stopped" if str(e) == "stopped" else f"failed: {e}")
        if str(e) != "stopped":
            host.logger.error(f"iqa_train: {e}")
        return summary
    finally:
        try:
            host.core.db_close()
        except Exception:
            pass
        progress.update(running=False, phase="idle", last=summary)
        _lock.release()


def start(host, **kw):
    if progress["running"]:
        return False
    threading.Thread(target=build, args=(host,), kwargs=kw, daemon=True, name="iqa-train").start()
    return True


def stop():
    _stop.set()
    return progress["running"]