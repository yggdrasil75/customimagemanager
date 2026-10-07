"""! @file
@brief Train HEURDUV (video), HEARDU (audio) and HEURDU 1.0 (animation).
======================================================================
The sibling of build.py for the timeline models. One build trains one
family's size series on the same stream of synthetic pairs:

  video  video_dataset: cached 1 fps clips -> pairs with step maps ->
         HEURDUV sizes (modules/dedup/seq_models.fit_batches), written to
         models/heurduv_<size>.pt (+ Ship: modules/dedup_cnn_video/pretrained)
  audio  audio_dataset: cached log-mels (+ real transcodes) -> pairs ->
         HEARDU sizes, models/heardu_<size>.pt (+ Ship: dedup_cnn_audio/pretrained)
  anim   video_dataset.anim_pairs: native-resolution runs of consecutive
         frames with per-cell change targets -> every size starts from the
         HEURDU 0.9 checkpoint of that size (local or HuggingFace), is
         upgraded to 1.0 (zero temporal block) and fine-tuned, written to
         models/heurdu1_<size>.pt (+ Ship: dedup_cnn/pretrained). The 0.9
         files are never touched; Settings > Models > HEURDU > Release
         switches to 1.0.

Held-out items are scored as whole pairs (score >= 0.5 vs the pair's
label from its map) per pair kind; feedback samples (the modules' sample
tables) get one pass per epoch after the synthetic ones. Runs on its own
thread; stoppable; one build at a time (shared with build.py's lock).
"""
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from . import build as bd
from . import video_dataset, audio_dataset
from modules.dedup import seq_models
from modules.dedup_cnn import dup_cnn as dc

_HERE = os.path.dirname(os.path.abspath(__file__))
KINDS = {
    "video": {"family": "HEURDUV", "prefix": "heurduv", "module": "dedup_cnn_video", "cap": "dedup.video"},
    "audio": {"family": "HEARDU", "prefix": "heardu", "module": "dedup_cnn_audio", "cap": "dedup.audio"},
    "anim":  {"family": "HEURDU 1.0", "prefix": "heurdu1", "module": "dedup_cnn", "cap": "dedup.pair"},
}
_stop = threading.Event()
progress = {"running": False, "kind": "", "phase": "", "items_total": 0, "items_done": 0, "pairs": 0,
            "epoch": 0, "epochs": 0, "loss": {}, "started": 0.0, "last": None, "error": None, "log": []}


def _say(host, msg):
    host.set_status("Dedup train: " + msg)
    host.logger.info("dedup_train(seq): " + msg)
    progress["log"] = (progress["log"] + [f"{time.strftime('%H:%M:%S')} {msg}"])[-200:]


def ship_dir(kind):
    return os.path.abspath(os.path.join(_HERE, "..", KINDS[kind]["module"], "pretrained"))


def _model_cls(kind):
    if kind == "video":
        from modules.dedup_cnn_video.heurduv import HEURDUV
        return HEURDUV
    if kind == "audio":
        from modules.dedup_cnn_audio.heardu import HEARDU
        return HEARDU
    return None


def default_sizes(kind):
    return dict(dc.SIZES) if kind == "anim" else dict(seq_models.SIZES)


def _heurdu09(host, size):
    """! @brief A HEURDU 0.9 checkpoint for `size` (local first, then HF) -> DupCNN, or an untrained one."""
    svc = host.get_service("dedup_cnn") or {}
    for p in (os.path.join(host.core.models_dir, f"dup_cnn_{size}.pt"),
              os.path.join(_HERE, "..", "dedup_cnn", "pretrained", f"dup_cnn_{size}.pt"),
              os.path.join(host.core.models_dir, "heurdu", f"dup_cnn_{size}.pt")):
        if os.path.exists(p):
            m = dc.DupCNN.load(p)
            if m.trained:
                return m, p
    try:
        import common
        from modules.dedup_cnn.module import HF_DEFAULT, HF_SIZES
        if size in HF_SIZES:
            spec = HF_DEFAULT.format(size=size).strip("/")
            owner, repo, *rest = spec.split("/")
            p = common.fetch_file(f"https://huggingface.co/{owner}/{repo}/resolve/main/{'/'.join(rest)}",
                                  os.path.join(host.core.models_dir, "heurdu", f"dup_cnn_{size}.pt"), min_bytes=1024)
            m = dc.DupCNN.load(p)
            if m.trained:
                return m, p
    except Exception:
        pass
    return dc.DupCNN.sized(size, svc.get("sizes", lambda: None)() if svc else None), None


def _feedback_pairs(host, kind):
    svc = host.get_service(f"dedup_{kind}_model")
    if not svc:
        return []
    try:
        rows = host.db().execute(f"SELECT blob,label FROM {svc['sample_table']}").fetchall()
    except Exception:
        return []
    out = []
    for blob, lab in rows:
        try:
            a, b, mp = seq_models.unpack_steps(blob)
            out.append((a, b, mp, "feedback"))
        except Exception:
            continue
    return out


def _eval(models, pairs, devs, kind):
    """! @brief {size: {pair kind: accuracy, all}} - whole-pair score >= 0.5 vs label."""
    is_dup = video_dataset.is_dup if kind != "audio" else audio_dataset.is_dup
    out = {}
    for z, m in models.items():
        if not m.trained or not pairs:
            continue
        ok, kinds = [], []
        for a, b, mp, k in pairs:
            s = m.score_steps(a, b, devs[z])
            if s is None:
                continue
            ok.append((s >= 0.5) == is_dup(mp)); kinds.append(k)
        ok, kinds = np.asarray(ok), np.asarray(kinds)
        rep = {k: round(float(ok[kinds == k].mean()), 3) for k in sorted(set(kinds))} if len(ok) else {}
        rep["all"] = round(float(ok.mean()), 3) if len(ok) else None
        out[z] = rep
    return out


def _eval_anim(models, pairs, devs):
    out = {}
    for z, m in models.items():
        if not m.trained or not pairs:
            continue
        ok = []
        for a, b, mk in pairs:
            y = 1.0 - mk.mean()
            s = m.score_animation(list(a), list(b), devs[z])
            if s is not None:
                ok.append((s >= 0.5) == (y >= 0.5))
        out[z] = {"all": round(float(np.mean(ok)), 3) if ok else None}
    return out


def build(host, kind, paths, sizes=None, active=None, max_items=20_000, per_item=4, epochs=3, chunk=64,
          batch=8, lr=1e-3, workers=4, holdout=0.05, seed=0, install=True, ship=False, device="",
          steps=32, use_feedback=True, on_installed=None):
    """! @brief Blocking build of one family (`kind` in KINDS). Returns the summary."""
    if kind not in KINDS:
        return {"ok": False, "error": f"unknown kind {kind!r}"}
    if not bd._lock.acquire(blocking=False):
        return {"ok": False, "error": "a build is already running"}
    if not dc._HAVE_TORCH:
        bd._lock.release()
        return {"ok": False, "error": "torch is not installed"}
    _stop.clear()
    progress.update(running=True, kind=kind, phase="caching", items_total=0, items_done=0, pairs=0, epoch=0,
                    epochs=int(epochs), loss={}, started=time.time(), error=None, log=[])
    sizes = dict(sizes or default_sizes(kind))
    active = active if active in sizes else list(sizes)[-1]
    fam = KINDS[kind]
    summary = {"ok": False, "kind": kind, "items": 0, "pairs": 0, "sizes": {z: {} for z in sizes}, "active": active}
    try:
        paths = list(paths)
        if not paths:
            raise RuntimeError(f"no {kind} files to learn from (empty library and no dataset folders)")
        rnd = random.Random(seed); rnd.shuffle(paths)
        paths = paths[:int(max_items)]
        rng = np.random.default_rng(seed)
        devs = bd.devices(device, {z: {"width": 1, "depth": 1} for z in sizes})
        # -- cache ----------------------------------------------------------
        if kind == "anim":
            items = paths                                   # decoded per pair (consecutive native frames)
        else:
            progress.update(items_total=len(paths))
            cache = video_dataset.cache_clip if kind == "video" else audio_dataset.cache_track
            items, n = [], 0
            with ThreadPoolExecutor(max(1, int(workers))) as ex:
                for cp in ex.map(lambda p: cache(host, p), paths):
                    if _stop.is_set():
                        raise RuntimeError("stopped")
                    if cp:
                        items.append(cp)
                    n += 1
                    if n % 20 == 0:
                        progress.update(items_done=n); _say(host, f"caching {n}/{len(paths)} ({len(items)} ok)")
        if len(items) < 2:
            raise RuntimeError(f"only {len(items)} usable {kind} file(s)")
        n_hold = max(4, int(len(items) * holdout)) if len(items) >= 20 else 0
        hold, train = items[:n_hold], items[n_hold:]
        summary["items"] = len(train)
        # -- models ---------------------------------------------------------
        cls = _model_cls(kind)
        models, opts, src = {}, {z: {} for z in sizes}, {}
        for z in sizes:
            if kind == "anim":
                m, p = _heurdu09(host, z)
                src[z] = p or "untrained"
                models[z] = m.upgrade()
            else:
                models[z] = cls(sizes[z]["width"], sizes[z]["depth"], size=z)
        _say(host, f"{fam['family']}: training {', '.join(sizes)} on {len(train)} items "
                   + (f"(from 0.9: {', '.join(f'{z}={os.path.basename(str(p))}' for z, p in src.items())})" if src else ""))
        fb = _feedback_pairs(host, kind) if (use_feedback and kind != "anim") else []
        summary["feedback_pairs"] = len(fb)

        def load(cp):
            if kind == "video":
                return np.load(cp, mmap_mode="r")
            return audio_dataset.load_track(cp)

        def make_pairs(chunk_items):
            if kind == "anim":
                ps = video_dataset.anim_pairs(chunk_items, rng, T=min(int(steps), dc.ANIM_MAX_FRAMES))
                return ps
            data = [load(cp) for cp in chunk_items]
            if kind == "video":
                return video_dataset.synth_pairs([np.ascontiguousarray(d) for d in data], rng, per_item, int(steps))
            return audio_dataset.synth_pairs(data, rng, per_item, int(steps))

        def train_all(pairs):
            if not pairs:
                return
            for z, m in models.items():
                if kind == "anim":
                    bs = int(batch)
                    for s in range(0, len(pairs), bs):
                        sl = pairs[s:s + bs]
                        T = min(len(a) for a, _b, _m in sl)
                        a = np.stack([p[0][:T] for p in sl]); b = np.stack([p[1][:T] for p in sl])
                        mk = np.stack([p[2][:T] for p in sl])
                        loss = m.fit_batches([(a, b, mk)], lr=lr, device=devs[z], _opt_holder=opts[z])
                else:
                    loss = m.fit_batches([[p[:3] for p in pairs[s:s + int(batch)]]
                                          for s in range(0, len(pairs), int(batch))],
                                         lr=lr, device=devs[z], _opt_holder=opts[z])
                if loss is not None:
                    progress["loss"][z] = round(loss, 4)

        progress.update(phase="training", items_total=len(train) * int(epochs), items_done=0)
        done = 0
        with ThreadPoolExecutor(1) as pre:
            for ep in range(int(epochs)):
                progress["epoch"] = ep + 1
                order = list(train); rnd.shuffle(order)
                chunks = [order[i:i + int(chunk)] for i in range(0, len(order), int(chunk))]
                fut = pre.submit(make_pairs, chunks[0])
                for ci, ch in enumerate(chunks):
                    if _stop.is_set():
                        raise RuntimeError("stopped")
                    pairs = fut.result()
                    fut = pre.submit(make_pairs, chunks[ci + 1]) if ci + 1 < len(chunks) else None
                    train_all(pairs)
                    done += len(ch); summary["pairs"] += len(pairs)
                    progress.update(items_done=done, pairs=summary["pairs"])
                    losses = " ".join(f"{z} {v}" for z, v in progress["loss"].items())
                    _say(host, f"epoch {ep + 1}/{epochs}, {done}/{progress['items_total']} items, "
                               f"{summary['pairs']} pairs; loss {losses}")
                if fb:
                    train_all(fb)
        progress["phase"] = "evaluating"
        held = {}
        if hold:
            hp = make_pairs(hold)
            held = _eval_anim(models, hp, devs) if kind == "anim" else _eval(models, hp, devs, kind)
        fbacc = _eval(models, fb, devs, kind) if fb else {}
        for z, m in models.items():
            summary["sizes"][z].update({"params": m.params, "device": devs[z], "final_loss": progress["loss"].get(z),
                                        "held_out": held.get(z, {}), "feedback": (fbacc.get(z) or {}).get("all"),
                                        "from": src.get(z), "bench_cpu": m.bench("cpu")})
        progress["phase"] = "writing"
        written = []
        for m in models.values():
            m.net.to("cpu")
        for do, base in ((install, host.core.models_dir), (ship, ship_dir(kind))):
            if not do:
                continue
            os.makedirs(base, exist_ok=True)
            for z, m in models.items():
                if m.trained:
                    p = os.path.join(base, f"{fam['prefix']}_{z}.pt")
                    if m.save(p):
                        written.append(p)
        summary["written"], summary["ok"] = written, bool(written)
        summary["installed"] = bool(install and written and on_installed and on_installed(kind, active))
        summary["seconds"] = round(time.time() - progress["started"])
        _say(host, f"done in {summary['seconds']} s: {summary['pairs']} pairs; held-out "
                   + " ".join(f"{z} {r['held_out'].get('all', '-')}" for z, r in summary["sizes"].items())
                   + f"; wrote {len(written)} file(s), active {active}")
        return summary
    except Exception as e:
        summary["error"] = str(e); progress["error"] = str(e)
        _say(host, "stopped" if str(e) == "stopped" else f"failed: {e}")
        if str(e) != "stopped":
            host.logger.error(f"dedup_train(seq): {e}", exc_info=True)
        return summary
    finally:
        progress.update(running=False, phase="idle", last=summary)
        bd._lock.release()


def start(host, **kw):
    if progress["running"] or bd.progress["running"]:
        return False
    threading.Thread(target=build, args=(host,), kwargs=kw, daemon=True, name="dedup-train-seq").start()
    return True


def stop():
    _stop.set()
    return progress["running"]
