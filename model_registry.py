"""
model_registry.py
=================
Process-wide load-on-demand cache for heavy models.

Every subsystem (faces, bodies, iqa, sam, seg, yolo, depth, cnn) loads its model
lazily and, without this, holds it for the process lifetime. A full scan touches
most of them, so resident memory becomes the sum of every model ever used. This
registry keeps loads lazy but makes them evictable: each model is registered once
with a loader/unloader and a rough cost; callers acquire it by key (loading on
first use), and after each load the least-recently-used entries are evicted until
back under budget.

Budgets are read from env at eviction time:
    CIM_MODEL_MAX_RESIDENT   max number of models kept live   (default 3)
    CIM_MODEL_VRAM_BUDGET_MB soft cap on summed GPU cost (MB)  (default 6000)
A budget of 0 disables that axis; both 0 means never evict.
"""

import os
import gc
import threading

try:
    import torch
except Exception:
    torch = None

MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")


def pin_cache_dir():
    """Point TORCH_HOME at MODELS_DIR/torch if the user hasn't set their own, so
    torch.hub/rtmlib/pyiqa downloads land under models/ and survive rebuilds.
    Idempotent. Returns the effective TORCH_HOME."""
    if not os.environ.get("TORCH_HOME"):
        try:
            th = os.path.join(MODELS_DIR, "torch")
            os.makedirs(th, exist_ok=True)
            os.environ["TORCH_HOME"] = th
        except Exception:
            pass
    return os.environ.get("TORCH_HOME")


pin_cache_dir()


def _env_int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except Exception:
        return default


class ModelRegistry:
    """Thread-safe LRU cache of heavy models. A failed load is cached as None
    (retried at most once); a failed unload is ignored. Eviction never touches
    the key currently being acquired."""

    def __init__(self):
        self._lock = threading.RLock()
        self._entries = {}
        self._seq = 0
        self._pinned = set()

    def _max_resident(self):
        return _env_int("CIM_MODEL_MAX_RESIDENT", 3)

    def _vram_budget_mb(self):
        return _env_int("CIM_MODEL_VRAM_BUDGET_MB", 6000)

    def register(self, key, loader, unloader=None, cost_mb=0, gpu=False):
        """Declare a model. Idempotent: re-registering keeps any live instance
        but refreshes loader/unloader/cost. loader() returns the model or None;
        unloader(model) frees it (default drops the ref and empties CUDA cache)."""
        with self._lock:
            e = self._entries.get(key)
            if e is None:
                self._entries[key] = {
                    "model": None, "loaded": False, "loader": loader,
                    "unloader": unloader, "cost_mb": cost_mb, "gpu": gpu,
                    "seq": 0, "err": ""}
            else:
                e.update(loader=loader, unloader=unloader,
                         cost_mb=cost_mb, gpu=gpu)
            return key

    def acquire(self, key):
        """Return the model for key, loading on first use. Touches LRU and runs
        eviction. None if unregistered or the loader failed."""
        with self._lock:
            e = self._entries.get(key)
            if e is None:
                return None
            self._pinned.add(key)
        try:
            with self._lock:
                if not e["loaded"]:
                    try:
                        e["model"] = e["loader"]()
                        e["err"] = ""
                    except Exception as ex:
                        e["model"] = None
                        e["err"] = repr(ex)
                    e["loaded"] = True
                self._seq += 1
                e["seq"] = self._seq
                model = e["model"]
            self._evict_over_budget(protect=key)
            return model
        finally:
            with self._lock:
                self._pinned.discard(key)

    def touch(self, key):
        """Mark key most-recently-used without forcing a load."""
        with self._lock:
            e = self._entries.get(key)
            if e and e["loaded"]:
                self._seq += 1
                e["seq"] = self._seq

    def unload(self, key):
        """Free one model now. Safe if never loaded."""
        with self._lock:
            self._unload_locked(key)

    def clear(self, prefix=None):
        """Free every model, or every key starting with prefix."""
        with self._lock:
            for k in [k for k in self._entries
                      if prefix is None or str(k).startswith(prefix)]:
                self._unload_locked(k)

    def status(self):
        """Snapshot: list of (key, loaded, cost_mb, gpu)."""
        with self._lock:
            return [(k, e["loaded"], e["cost_mb"], e["gpu"])
                    for k, e in self._entries.items()]

    def _unload_locked(self, key):
        e = self._entries.get(key)
        if not e or not e["loaded"]:
            return
        model = e["model"]
        e["model"] = None
        e["loaded"] = False
        if model is None:
            return
        try:
            if e["unloader"]:
                e["unloader"](model)
            else:
                del model
        except Exception:
            pass
        finally:
            gc.collect()
            if torch is not None and e["gpu"]:
                try:
                    torch.cuda.empty_cache()
                except Exception:
                    pass

    def _evict_over_budget(self, protect=None):
        max_n = self._max_resident()
        vram = self._vram_budget_mb()
        if max_n <= 0 and vram <= 0:
            return
        while True:
            with self._lock:
                live = [(e["seq"], k, e) for k, e in self._entries.items()
                        if e["loaded"] and e["model"] is not None]
                if not live:
                    return
                over_count = max_n > 0 and len(live) > max_n
                gpu_mb = sum(e["cost_mb"] for _, _, e in live if e["gpu"])
                over_vram = vram > 0 and gpu_mb > vram
                if not over_count and not over_vram:
                    return
                live.sort(key=lambda t: t[0])
                victim = next((k for _, k, _e in live
                               if k != protect and k not in self._pinned), None)
                if victim is None:
                    return
                self._unload_locked(victim)


REGISTRY = ModelRegistry()

register = REGISTRY.register
acquire = REGISTRY.acquire
touch = REGISTRY.touch
unload = REGISTRY.unload
clear = REGISTRY.clear
status = REGISTRY.status