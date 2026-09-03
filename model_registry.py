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

def _torch_hip_version():
    if torch is None:
        return None
    try:
        return getattr(getattr(torch, "version", None), "hip", None)
    except Exception:
        return None


def _detect_backend():
    override = (os.environ.get("CIM_DEVICE") or "").strip().lower()
    if override in ("cpu",):
        return "cpu"
    if torch is not None:
        try:
            if torch.cuda.is_available():
                if _torch_hip_version():
                    return "rocm"
                return "cuda"
        except Exception:
            pass
    return "cpu"


def backend_reason():
    """One-line explanation of why backend() decided what it did, for logs and
    the UI. Every CPU fallback in _detect_backend is silent and they look
    identical from outside, so name the branch that actually fired."""
    override = (os.environ.get("CIM_DEVICE") or "").strip().lower()
    if override in ("cpu",):
        return "CPU: forced by CIM_DEVICE=cpu"
    if torch is None:
        return f"CPU: torch did not import ({_TORCH_IMPORT_ERROR or 'unknown'})"
    try:
        if not torch.cuda.is_available():
            hip = _torch_hip_version()
            return ("CPU: torch.cuda.is_available()=False, "
                    + (f"torch is a ROCm build (HIP {hip}) so the runtime isn't "
                       "seeing the GPU" if hip else
                       "torch is NOT a ROCm build (no HIP) — wrong wheel for this image"))
    except Exception as e:
        return f"CPU: probing torch.cuda failed ({type(e).__name__}: {e})"
    hip = _torch_hip_version()
    try:
        name = torch.cuda.get_device_name(0)
    except Exception:
        name = "?"
    return f"{'ROCm' if hip else 'CUDA'}: {name}"


def log_backend(log):
    """Dump everything that decides GPU-vs-CPU, once, at startup.

    This runs in-process so it reflects what the app actually sees — the same
    interpreter, env and device nodes the scan will use. Hand-running a probe in
    a shell inside the container can disagree with the real process, which is
    exactly when you'd be chasing the wrong thing.
    """
    # A silent CPU fallback isn't a status note — for this workload it's ~100x
    # slower and it's the thing you'll be hunting. Emit the WHOLE dump at ERROR so
    # it lands in error.log as one readable block; a healthy GPU is just INFO.
    cpu = backend() == "cpu"
    emit = log.error if cpu else log.info
    emit("gpu: %s (%s)", "running on CPU" if cpu else f"backend={backend()}",
         backend_reason())
    for k in ("CIM_DEVICE", "HIP_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES",
              "HSA_OVERRIDE_GFX_VERSION", "CUDA_VISIBLE_DEVICES"):
        v = os.environ.get(k)
        if v:
            emit("gpu: env %s=%s", k, v)
    # ROCm needs both device nodes passed into the container AND the process must
    # be able to OPEN them. os.path.exists() is not enough: the classic failure is
    # a node that is present but unreadable because the container user isn't in
    # the video/render groups. That enumerates as device_count=0 — identical
    # symptom to having no GPU at all, which is why existence alone misleads.
    try:
        import glob as _g
        def _acc(p):
            return "ok" if os.access(p, os.R_OK | os.W_OK) else "PERMISSION DENIED"
        kfd = "/dev/kfd"
        emit("gpu: %s exists=%s access=%s", kfd, os.path.exists(kfd),
             _acc(kfd) if os.path.exists(kfd) else "n/a")
        for p in sorted(_g.glob("/dev/dri/renderD*")):
            emit("gpu: %s access=%s", p, _acc(p))
        try:
            st = os.stat(kfd)
            emit("gpu: %s owner uid=%s gid=%s mode=%o",
                 kfd, st.st_uid, st.st_gid, st.st_mode & 0o777)
        except Exception:
            pass
        emit("gpu: process uid=%s gid=%s groups=%s",
             os.getuid(), os.getgid(), sorted(os.getgroups()))
        # What the amdgpu/kfd KERNEL driver enumerates, independent of torch. If
        # agents show up here but torch still reports 0 devices, the kernel is
        # fine and ROCm userspace is rejecting the card — usually an unsupported
        # gfx target, which HSA_OVERRIDE_GFX_VERSION exists to work around.
        for nd in sorted(_g.glob("/sys/class/kfd/kfd/topology/nodes/*/properties")):
            try:
                props = dict(
                    ln.split(None, 1) for ln in open(nd).read().splitlines()
                    if len(ln.split(None, 1)) == 2)
                gfx = props.get("gfx_target_version", "0").strip()
                simd = props.get("simd_count", "0").strip()
                if gfx != "0" and simd != "0":     # 0/0 == the CPU node, skip
                    g = int(gfx)
                    maj, mnr, stp = g // 10000, (g // 100) % 100, g % 100
                    emit("gpu: kfd agent %s = gfx%d%x%x (HSA_OVERRIDE_GFX_VERSION=%d.%d.%d) simd_count=%s",
                         nd.split("/")[-2], maj, mnr, stp, maj, mnr, stp, simd)
            except Exception:
                continue
    except Exception:
        pass
    if torch is None:
        emit("gpu: torch not importable (%s)", _TORCH_IMPORT_ERROR or "unknown")
        return
    try:
        emit("gpu: torch=%s hip=%s cuda.is_available=%s device_count=%s",
                 torch.__version__, getattr(torch.version, "hip", None),
                 torch.cuda.is_available(), torch.cuda.device_count())
    except Exception as e:
        emit("gpu: torch probe failed (%s: %s)", type(e).__name__, e)
    try:
        import onnxruntime as _ort
        provs = _ort.get_available_providers()
        want = {"cuda": "CUDAExecutionProvider", "rocm": "ROCMExecutionProvider"}.get(backend())
        emit("gpu: onnxruntime=%s providers=%s", _ort.__version__, provs)
        if want and want not in provs:
            log.error("gpu: onnxruntime is missing %s on a %s backend — face "
                      "detection will run on CPU (~100x slower). Install the GPU "
                      "onnxruntime build for this backend.", want, backend())
    except Exception as e:
        emit("gpu: onnxruntime probe failed (%s: %s)", type(e).__name__, e)


_BACKEND = None
_DEVICE = None

def backend():
    """The detected accelerator vendor: 'cuda' (NVIDIA), 'rocm' (AMD), or 'cpu'.
    Decided once, process-wide. Use this when the vendor matters (ONNX providers,
    logging). For the torch device string, use device()."""
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = _detect_backend()
    return _BACKEND

def device():
    global _DEVICE
    if _DEVICE is None:
        _DEVICE = "cuda" if backend() in ("cuda", "rocm") else "cpu"
    return _DEVICE

def on_gpu():
    """True when a GPU accelerator (CUDA or ROCm) was chosen. Convenience for
    loaders that only care 'GPU vs CPU', not the vendor."""
    return device() == "cuda"

def onnx_providers():
    """The ONNX Runtime execution-provider preference list for the detected
    backend, most-preferred first, always ending in CPUExecutionProvider so a
    session still builds if the GPU provider isn't in the installed onnxruntime
    wheel. Any library that constructs an onnxruntime InferenceSession (insight-
    face, rtmlib, rapidocr, ...) should pass this instead of hardcoding CUDA.

    Filtered against onnxruntime.get_available_providers() when that import is
    cheap, so we never hand ORT a provider it doesn't have and trigger its noisy
    fallback warning; if onnxruntime isn't importable we return the unfiltered
    preference and let the caller deal with it."""
    b = backend()
    if b == "rocm":
        pref = ["ROCMExecutionProvider", "MIGraphXExecutionProvider",
                "CPUExecutionProvider"]
    elif b == "cuda":
        pref = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    else:
        return ["CPUExecutionProvider"]
    try:
        import onnxruntime as ort
        avail = set(ort.get_available_providers())
        filtered = [p for p in pref if p in avail]
        # Guarantee CPU is always present as the final fallback.
        if "CPUExecutionProvider" not in filtered:
            filtered.append("CPUExecutionProvider")
        return filtered
    except Exception:
        return pref

def onnx_device_id():
    """ctx/device id for ONNX-style APIs that take an int (insightface's ctx_id,
    rtmlib device string suffix, ...): 0 on a GPU backend, -1 on CPU."""
    return 0 if on_gpu() else -1

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