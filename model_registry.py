import os
import gc
import threading
import contextlib
import sys

try:
    import torch
except Exception:
    torch = None

VRAM_BUDGET_FRAC = 0.85

def _detected_vram_mb():
    if torch is None:
        return 0.0
    try:
        if not torch.cuda.is_available():
            return 0.0
        return torch.cuda.get_device_properties(0).total_memory / (1024 * 1024)
    except Exception:
        return 0.0

def _vram_budget_mb():
    v = _detected_vram_mb()
    return int(v * VRAM_BUDGET_FRAC) if v > 0 else 0

def _max_resident_for_vram():
    v = _detected_vram_mb()
    if v <= 0:
        return 0
    return max(2, min(8, int(v / 2560)))

def _system_ram_mb():
    if psutil is not None:
        try:
            return psutil.virtual_memory().total / (1024 * 1024)
        except Exception:
            pass
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        return pages * (os.sysconf("SC_PAGE_SIZE") / (1024 * 1024))
    except Exception:
        return 0.0

try:
    import psutil
except Exception:
    psutil = None

MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")

def _model_device(model):
    if model is None:
        return None
    try:
        d = getattr(model, "device", None)
        if d is not None:
            return str(d)
    except Exception:
        pass
    for obj in (model, getattr(model, "model", None)):
        if obj is None:
            continue
        try:
            params = obj.parameters()
            first = next(params, None)
            if first is not None:
                return str(first.device)
        except Exception:
            continue
    return None


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

def _detect_device():
    if torch is not None:
        try:
            if torch.cuda.is_available():
                return "cuda"
        except Exception:
            pass
    return "cpu"

_DEVICE = None

def device():
    """The process-wide compute device string ('cpu' or 'cuda'), decided once by
    the registry. Loaders call this instead of probing torch themselves, so the
    choice is made in exactly one place."""
    global _DEVICE
    if _DEVICE is None:
        _DEVICE = _detect_device()
    return _DEVICE

def on_gpu():
    """True when the chosen device is CUDA. Convenience for loaders."""
    return device() == "cuda"

def _rss_mb():
    if psutil is not None:
        try:
            return psutil.Process().memory_info().rss / (1024 * 1024)
        except Exception:
            pass
    try:
        with open(f"/proc/{os.getpid()}/statm") as f:
            pages = int(f.read().split()[1])
        return pages * (os.sysconf("SC_PAGE_SIZE") / (1024 * 1024))
    except Exception:
        return 0.0

def _vram_mb():
    if torch is None:
        return 0.0
    try:
        return torch.cuda.memory_allocated() / (1024 * 1024)
    except Exception:
        return 0.0

def _mem_snapshot():
    """(rss_mb, vram_mb) before a load, for measuring what it actually cost."""
    return (_rss_mb(), _vram_mb())

def _measure_cost(before, dev):
    """Actual MB a load consumed, measured as the delta from `before`. Uses the
    VRAM delta when the model landed on CUDA (dedicated card), else the RSS delta.
    This is what makes cost_mb self-calibrating: the declared value is only a
    pre-load estimate; once loaded we know the truth and store it. Returns 0 when
    the delta is non-positive (measurement noise / shared pages) so the caller
    keeps the declared estimate."""
    rss0, vram0 = before
    on_cuda = dev is not None and str(dev).lower().startswith(("cuda", "gpu"))
    if on_cuda:
        d = _vram_mb() - vram0
        if d > 1.0:
            return d
        # VRAM delta unreliable (allocator caching / non-torch backend) — fall
        # back to RSS delta, which still moves for host-side buffers.
    d = _rss_mb() - rss0
    return d if d > 1.0 else 0.0

def _file_cost_mb(model_path):
    if not model_path:
        return 0.0
    p = model_path
    if not os.path.isabs(p) and not os.path.dirname(p):
        p = os.path.join(MODELS_DIR, p)
    for cand in (model_path, p, os.path.join(MODELS_DIR, os.path.basename(model_path))):
        try:
            if os.path.isfile(cand):
                return os.path.getsize(cand) / (1024 * 1024)
        except Exception:
            continue
    return 0.0

class ModelRegistry:
    """Thread-safe LRU cache of heavy models. A failed load is cached as None
    (retried at most once); a failed unload is ignored. Eviction never touches
    the key currently being acquired."""

    def __init__(self):
        self._lock = threading.RLock()
        self._entries = {}
        self._seq = 0
        self._pinned = set()
        self._leased = {}
        self._load_locks = {}
    def _load_lock_for(self, key):
        with self._lock:
            lk = self._load_locks.get(key)
            if lk is None:
                lk = self._load_locks[key] = threading.Lock()
            return lk

    def _max_resident(self):
        return _max_resident_for_vram()

    def _vram_budget_mb(self):
        return _vram_budget_mb()

    def register(self, key, loader, unloader=None, cost_mb=0, gpu=False,
                 model_path=None):
        est = _file_cost_mb(model_path) if model_path else 0.0
        if est <= 0:
            est = cost_mb
        with self._lock:
            e = self._entries.get(key)
            if e is None:
                self._entries[key] = {
                    "model": None, "loaded": False, "loader": loader,
                    "unloader": unloader, "cost_mb": est, "gpu": gpu,
                    "seq": 0, "err": ""}
            else:
                new_cost = e["cost_mb"] if e.get("measured") else est
                e.update(loader=loader, unloader=unloader,
                         cost_mb=new_cost, gpu=gpu)
            return key

    def acquire(self, key):
        with self._lock:
            e = self._entries.get(key)
            if e is None:
                return None
            self._pinned.add(key)
            already = e["loaded"]
            model = e["model"]
        try:
            if not already:
                lk = self._load_lock_for(key)
                with lk:
                    with self._lock:
                        loaded = e["loaded"]
                        model = e["model"]
                    if not loaded:
                        print(f"REGBUILD key={key} entry_id={id(e)} entries_id={id(self._entries.get(key))} loaded={e['loaded']} nkeys={len(self._entries)}", file=sys.stderr, flush=True)
                        hook = getattr(self, "_mem_hook", None)
                        res = hook(e["cost_mb"], e["gpu"]) if hook else None
                        if res is not None:
                            res.__enter__()
                        before = _mem_snapshot()
                        built = None
                        err = ""
                        try:
                            built = e["loader"]()
                        except Exception as ex:
                            err = repr(ex)
                        dev = _model_device(built)
                        if res is not None and dev is not None and hasattr(res, "retarget"):
                            res.retarget(dev)
                        measured = _measure_cost(before, dev)
                        with self._lock:
                            e["model"] = built
                            e["err"] = err
                            e["loaded"] = True
                            print(f"REGSTORE key={key} entry_id={id(e)} built_is_none={built is None} err={err[:60]}", file=__import__('sys').stderr, flush=True)
                            if measured and measured > 0:
                                e["cost_mb"] = measured
                                e["measured"] = True
                            if res is not None:
                                e["mem_res"] = res
                        if res is not None:
                            if measured and measured > 0 and hasattr(res, "resize"):
                                res.resize(measured)
                            res.settle()
                        model = built
            with self._lock:
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

    def hold(self, *keys):
        """Pin one or more keys resident until release(). Refcounted, so paired
        hold/release nest safely. Prefer the lease() context manager below."""
        with self._lock:
            for k in keys:
                self._leased[k] = self._leased.get(k, 0) + 1

    def release(self, *keys):
        """Undo one hold() for each key; the key becomes evictable again once its
        refcount hits zero. After release, trim anything now over budget."""
        with self._lock:
            for k in keys:
                n = self._leased.get(k, 0) - 1
                if n <= 0:
                    self._leased.pop(k, None)
                else:
                    self._leased[k] = n
        self._evict_over_budget()

    @contextlib.contextmanager
    def lease(self, *keys):
        self.hold(*keys)
        try:
            yield
        finally:
            self.release(*keys)

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

    def set_memory_hook(self, hook):
        """Install a callable hook(cost_mb, gpu) -> reservation context manager,
        used to reserve a model's memory through the thread manager on load. The
        reservation must support __enter__/__exit__ and a settle() method (RAM
        reservations settle after load; VRAM reservations are held until the
        model is unloaded). None disables reservation. Kept optional so the
        registry has no hard dependency on the thread manager."""
        with self._lock:
            self._mem_hook = hook

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
        res = e.pop("mem_res", None)
        if res is not None:
            try:
                res.__exit__(None, None, None)   # frees any held VRAM reservation
            except Exception:
                pass
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
        vram = self._vram_budget_mb()
        max_n = self._max_resident()
        if vram <= 0:
            return
        while True:
            with self._lock:
                live = [(e["seq"], k, e) for k, e in self._entries.items()
                        if e["loaded"] and e["model"] is not None and e["gpu"]]
                if not live:
                    return
                over_count = max_n > 0 and len(live) > max_n
                gpu_mb = sum(e["cost_mb"] for _, _, e in live)
                over_vram = gpu_mb > vram
                if not over_count and not over_vram:
                    return
                live.sort(key=lambda t: t[0])
                victim = next((k for _, k, _e in live
                               if k != protect and k not in self._pinned
                               and k not in self._leased), None)
                if victim is None:
                    return
                self._unload_locked(victim)

REGISTRY = ModelRegistry()

register = REGISTRY.register
acquire = REGISTRY.acquire
touch = REGISTRY.touch
unload = REGISTRY.unload
lease = REGISTRY.lease
hold = REGISTRY.hold
release = REGISTRY.release
clear = REGISTRY.clear
status = REGISTRY.status
set_memory_hook = REGISTRY.set_memory_hook