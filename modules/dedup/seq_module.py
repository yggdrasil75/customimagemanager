"""! @file
@brief Shared module plumbing for the sequence scorers (HEURDUV, HEARDU).
======================================================================
dedup_cnn_video and dedup_cnn_audio are the same shape as dedup_cnn: a model
capability picked in Settings > Models (sizes downloaded from HuggingFace
on first use, a size trained in Trainer > Dedup overriding the download),
a pair-scorer in the dedup registry for ONE media kind, a feedback sample
table, and a service (reload / status / record). This file is that shape
once; each module passes its model class and names.

Checkpoint lookup per size, first hit wins:
    models/<prefix>_<size>.pt                (Trainer > Dedup install)
    modules/<module>/pretrained/<prefix>_<size>.pt   (Trainer "Ship")
    models/<prefix>/<prefix>_<size>.pt       (previous HF download)
    https://huggingface.co/<repo>/resolve/main/<FAMILY>_<size>.pt
A size with no checkpoint anywhere makes the scorer unavailable for 10
minutes (the reason is logged once), so the next scorer - the legacy model
or the naive score - answers meanwhile.
"""

import os
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict

import common

from . import seq_models

_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    blob    BLOB NOT NULL,
    label   INTEGER NOT NULL,
    created REAL NOT NULL
);
"""


def path_to_map(path, n_b: int):
    """! @brief DTW path [(i, j)] -> map [n_b]: the first a-step aligned to each b-step."""
    import numpy as np
    mp = np.full(n_b, -1, np.int64)
    for i, j in path:
        if mp[j] < 0:
            mp[j] = i
    return mp


def register_seq_module(host, *, cls, kind, cap, cap_label, cap_summary, cap_input, prefix,
                        hf_repo, hf_sizes, sample_table, module_dir, label, note,
                        map_fn, priority=30, cost_mb=32):
    """! @brief Register a sequence scorer module. `map_fn(steps_a, steps_b, abs_a, abs_b)`
    -> step map for a merged (duplicate) feedback pair."""
    scorers = host.get_service("dedup_scorers")
    if scorers is None:
        host.logger.info(f"{prefix}: dedup registry unavailable; skipping")
        return None
    host.add_table(_DDL.format(table=sample_table))
    models_dir = os.path.abspath(host.core.models_dir)
    pretrained = os.path.join(module_dir, "pretrained")
    sizes_key = f"{prefix}_sizes"
    host.add_config_key(sizes_key, default=seq_models.sizes_text(cls.SIZES), validate=lambda v: str(v or ""))
    host.declare_capability(cap, label=cap_label, summary=cap_summary, input=cap_input, output="float 0..1")
    loaded, failed = {}, {}
    emb_cache, emb_lock = OrderedDict(), threading.Lock()

    def _local_paths(size):
        return [os.path.join(models_dir, f"{prefix}_{size}.pt"),
                os.path.join(pretrained, f"{prefix}_{size}.pt"),
                os.path.join(models_dir, prefix, f"{prefix}_{size}.pt")]

    def _local_sizes():
        out = []
        for d in (models_dir, pretrained):
            try:
                out += [f[len(prefix) + 1:-3] for f in sorted(os.listdir(d))
                        if f.startswith(prefix + "_") and f.endswith(".pt") and not f.endswith(".ckpt.pt")]
            except OSError:
                pass
        return out

    def _all_sizes():
        return list(dict.fromkeys(list(hf_sizes) + list(seq_models.parse_sizes(host.config.get(sizes_key)))
                                  + _local_sizes()))

    def _path_for(size):
        for p in _local_paths(size):
            if os.path.exists(p):
                return p
        if size not in hf_sizes:
            raise RuntimeError(f"no trained {cls.FAMILY} checkpoint for size '{size}'; train it in Trainer > Dedup")
        return common.fetch_file(_url(size), _local_paths(size)[2], min_bytes=1024)

    def _size():
        return str(host.model_variant(cap)["size"] or "medium")

    remote = {}  # size -> (ok, why, checked_at): HEAD probe of the download URL

    def _url(size):
        return f"https://huggingface.co/{hf_repo}/resolve/main/{cls.FAMILY}_{size}.pt"

    def _remote_ok(size):
        """! @brief True when the size's download URL answers (a success is kept, a miss retried after 10 min)."""
        ok, why, at = remote.get(size, (None, "", 0.0))
        if ok or (ok is False and time.time() - at < 600):
            return ok
        try:
            req = urllib.request.Request(_url(size), method="HEAD")
            with urllib.request.urlopen(req, timeout=10) as r:
                ok, why = 200 <= r.status < 400, f"HTTP {r.status}"
        except urllib.error.HTTPError as e:
            ok, why = False, f"HTTP {e.code} {e.reason}"
        except Exception as e:
            ok, why = False, f"{type(e).__name__}: {e}"
        remote[size] = (ok, why, time.time())
        return ok

    def _available():
        if not seq_models._HAVE_TORCH:
            return False
        size = _size()
        if any(os.path.exists(p) for p in _local_paths(size)):
            return True
        return size in hf_sizes and _remote_ok(size)

    def _reason():
        if not seq_models._HAVE_TORCH:
            return "needs torch"
        size = _size()
        if size not in hf_sizes:
            return f"no trained {cls.FAMILY} checkpoint for size '{size}'; train it in Trainer > Dedup"
        return (f"{cls.FAMILY} {size}: no local checkpoint and the download failed "
                f"({remote.get(size, (None, 'not checked', 0))[1]}): {_url(size)}")

    def _device():
        return "cuda" if seq_models._HAVE_TORCH and seq_models.torch.cuda.is_available() else "cpu"

    def _loader(cap_=cap):
        size = _size()
        m = loaded.get(size)
        if m is None:
            path = _path_for(size)
            m = cls.load(path)
            if not m.trained:
                raise RuntimeError(f"{cls.FAMILY} {size}: {path} did not load: {m.error or 'unknown error'}")
            loaded[size] = m
            host.logger.info(f"{prefix}: loaded {cls.FAMILY} {size} from {path} ({m.params} params)")
        return lambda a, b: m.score_paths(a, b, _device())

    host.provide_model(cap, prefix, label=cls.FAMILY, family=cls.FAMILY, sizes=_all_sizes(), loader=_loader,
                       available=_available, reason=_reason,
                       cost_mb=cost_mb, gpu=False, supports_conf=False, note=note)

    def _model():
        size = _size()
        until, why = failed.get(size, (0, ""))
        if until > time.time():
            raise RuntimeError(why)
        m = loaded.get(size)
        if m is not None:
            return m
        try:
            host.request_model(cap)
        except Exception as e:
            why = f"no {cls.FAMILY} model for size '{size}': {type(e).__name__}: {e}"
            failed[size] = (time.time() + 600, why)
            host.logger.warning(f"{prefix}: {why} (check Settings > Models > {cls.FAMILY}; retrying in 10 min)")
            raise RuntimeError(why)
        m = loaded.get(size)
        if m is None:
            raise RuntimeError(f"{cls.FAMILY} size '{size}' was requested but not registered as loaded")
        return m

    def _available():
        if not seq_models._HAVE_TORCH:
            return False
        return failed.get(_size(), (0, ""))[0] <= time.time()

    def _stamp(size):
        for p in _local_paths(size):
            if os.path.exists(p):
                return str(int(os.path.getmtime(p)))
        return "0"

    def _tag():
        size = _size()
        return f"{prefix}:{size}:{_stamp(size)}"

    def _embed(m, path, tag):
        """! @brief Embedding of one file, cached (a group of N scores N-1 pairs per member)."""
        try:
            key = (path, os.path.getmtime(path), tag)
        except OSError:
            return None
        with emb_lock:
            if key in emb_cache:
                emb_cache.move_to_end(key)
                return emb_cache[key]
        steps = cls.steps_from_path(path)
        e = m.embed(steps, _device()) if steps is not None and len(steps) else None
        with emb_lock:
            emb_cache[key] = e
            while len(emb_cache) > 64:
                emb_cache.popitem(last=False)
        return e

    def _score_batch(ctxs):
        m = _model()                      # raises with the reason: the registry records it
        tag = _tag()
        out = []
        for c in ctxs:
            pa, pb = c.get("ref_path"), c.get("other_path")
            if not pa or not pb:
                out.append(None); continue
            ea, eb = _embed(m, pa, tag), _embed(m, pb, tag)
            if ea is None or eb is None:
                out.append(None); continue
            from .seq_align import dtw_score
            out.append(dtw_score(1.0 - m.sim_matrix(ea, eb)))
        return out

    scorers.register({"id": prefix, "label": cls.FAMILY, "available": _available, "priority": priority,
                      "score": lambda ctx: _score_batch([ctx])[0], "score_batch": _score_batch,
                      "tag": _tag, "kinds": (kind,)})

    def _reload(active=None):
        loaded.clear(); failed.clear()
        with emb_lock:
            emb_cache.clear()
        try:
            prov = host.broker._providers.get(cap, {}).get(prefix)
            if prov is not None:
                prov.sizes = _all_sizes()
        except Exception as e:
            host.logger.warning(f"{prefix}: refresh sizes: {e}")
        if active:
            ok, err = host.broker.select(cap, prefix, str(active))
            if ok:
                try:
                    host.persist_model_selection()
                except Exception as e:
                    host.logger.warning(f"{prefix}: save selection: {e}")
            else:
                host.logger.warning(f"{prefix}: could not select size '{active}': {err}")
        return True

    def _status():
        size = _size()
        m = loaded.get(size)
        return {"available": bool(seq_models._HAVE_TORCH), "trained": bool(m and m.trained),
                "error": failed.get(size, (0, ""))[1], "size": size, "params": m.params if m else 0}

    def _record(abs_a, abs_b, label):
        """! @brief Store a merge (1) / not-a-duplicate (0) decision as a training sample."""
        try:
            sa, sb = cls.steps_from_path(abs_a), cls.steps_from_path(abs_b)
            if sa is None or sb is None or not len(sa) or not len(sb):
                return False
            import numpy as np
            mp = map_fn(sa, sb, abs_a, abs_b) if int(label) else np.full(len(sb), -1, np.int64)
            db = host.db()
            db.execute(f"INSERT INTO {sample_table}(blob,label,created) VALUES(?,?,?)",
                       (seq_models.pack_steps(sa, sb, mp), int(label), time.time()))
            db.commit()
            return True
        except Exception as e:
            host.logger.warning(f"{prefix} record sample: {e}")
            return False

    svc = {"reload": _reload, "status": _status, "record": _record, "kind": kind, "family": cls.FAMILY,
           "sizes": lambda: seq_models.parse_sizes(host.config.get(sizes_key)), "sizes_key": sizes_key,
           "sample_table": sample_table, "prefix": prefix, "pretrained": pretrained, "cls": cls}
    host.provide_service(f"dedup_{kind}_model", svc)
    host.logger.info(f"{prefix}: registered {cls.FAMILY} scorer for '{kind}' ({cap})")
    return svc
