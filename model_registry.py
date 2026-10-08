"""! @file
@brief Model files, accelerator detection (CUDA / ROCm / CPU, ONNX providers) and
the LRU cache that keeps heavy models within the memory budget."""
import os
import gc
import threading
import contextlib
import sys

_TORCH_IMPORT_ERROR = ""
try:
    import torch
except Exception as _e:
    torch = None
    _TORCH_IMPORT_ERROR = str(_e)

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

# Every downloaded model and library cache lives under here; CIM_MODELS_DIR
# moves the whole tree (nothing should land in ~/.cache on the OS disk).
MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")

def model_dir(backend, chore):
    """! @brief models/<backend>/<chore>/, created on first use.
    @param chore  capability id without dots (detect, detectobb, segment, pose, ...).
    """
    d = os.path.join(MODELS_DIR, backend, str(chore).replace(".", ""))
    os.makedirs(d, exist_ok=True)
    return d

def list_weights(backend, chore, exts=(".pt", ".pth")):
    """! @brief Weight files under models/<backend>/<chore>/."""
    d = model_dir(backend, chore)
    return sorted(os.path.join(d, f) for f in os.listdir(d)
                  if f.lower().endswith(tuple(exts)))

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


# Library caches redirected under MODELS_DIR unless already set. Must run before
# those libraries import (they read the environment then):
#   TORCH_HOME  torch.hub, rtmlib, pyiqa checkpoints
#   HF_HOME     Hugging Face snapshots and the xet chunk cache
_PINNED = {"TORCH_HOME": "torch", "HF_HOME": "huggingface"}


def pin_cache_dir():
    """! @brief Point each library cache at MODELS_DIR/<sub> unless the user set it.
    @return {variable: effective path}.
    """
    out = {}
    for var, sub in _PINNED.items():
        if not os.environ.get(var):
            try:
                d = os.path.join(MODELS_DIR, sub)
                os.makedirs(d, exist_ok=True)
                os.environ[var] = d
            except Exception:
                pass
        out[var] = os.environ.get(var)
    return out

pin_cache_dir()

def _torch_hip_version():
    if torch is None:
        return None
    try:
        return getattr(getattr(torch, "version", None), "hip", None)
    except Exception:
        return None


def _detect_backend():
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
    """! @brief Which branch of the backend detection fired (every CPU fallback looks alike otherwise)."""
    if torch is None:
        return f"CPU: torch did not import ({_TORCH_IMPORT_ERROR or 'unknown'})"
    try:
        if not torch.cuda.is_available():
            hip = _torch_hip_version()
            return ("CPU: torch.cuda.is_available()=False, "
                    + (f"torch is a ROCm build (HIP {hip}) so the runtime isn't "
                       "seeing the GPU" if hip else
                       "torch is NOT a ROCm build (no HIP) - wrong wheel for this image"))
    except Exception as e:
        return f"CPU: probing torch.cuda failed ({type(e).__name__}: {e})"
    hip = _torch_hip_version()
    try:
        name = torch.cuda.get_device_name(0)
    except Exception:
        name = "?"
    return f"{'ROCm' if hip else 'CUDA'}: {name}"


def log_backend(log):
    """! @brief Log everything that decides GPU vs CPU, once at startup, from inside
    the real process (a shell probe can see a different environment).
    """
    # A silent CPU fallback is ~100x slower: log it at ERROR as one block.
    cpu = backend() == "cpu"
    emit = log.error if cpu else log.info
    emit("gpu: %s (%s)", "running on CPU" if cpu else f"backend={backend()}",
         backend_reason())
    for k in ("HIP_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES",
              "HSA_OVERRIDE_GFX_VERSION", "CUDA_VISIBLE_DEVICES"):
        v = os.environ.get(k)
        if v:
            emit("gpu: env %s=%s", k, v)
    # ROCm needs the device nodes present AND openable; a node the container user
    # can't open (not in video/render) looks exactly like having no GPU.
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
        # What the kernel driver enumerates, independent of torch. Agents here but no
        # torch devices usually means an unsupported gfx target (HSA_OVERRIDE_GFX_VERSION).
        for nd in sorted(_g.glob("/sys/class/kfd/kfd/topology/nodes/*/properties")):
            try:
                props = dict(
                    ln.split(None, 1) for ln in open(nd).read().splitlines()
                    if len(ln.split(None, 1)) == 2)
                gfx = props.get("gfx_target_version", "0").strip()
                simd = props.get("simd_count", "0").strip()
                if gfx != "0" and simd != "0":  # 0/0 is the CPU node
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
        want = {"cuda": "CUDAExecutionProvider", "rocm": "MIGraphXExecutionProvider"}.get(backend())
        emit("gpu: onnxruntime=%s providers=%s", _ort.__version__, provs)
        if want and want not in provs:
            log.error("gpu: onnxruntime is missing %s on a %s backend - face "
                      "detection will run on CPU (~100x slower). Install the GPU "
                      "onnxruntime build for this backend.", want, backend())
    except Exception as e:
        emit("gpu: onnxruntime probe failed (%s: %s)", type(e).__name__, e)


_BACKEND = None
_DEVICE = None

def backend():
    """! @brief The accelerator vendor, decided once: "cuda", "rocm" or "cpu".
    For the torch device string use device().
    """
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
    """! @brief True when a GPU (CUDA or ROCm) was chosen."""
    return device() == "cuda"

_DEVICES = None

def available_devices():
    """! @brief Devices for the UI's device picker, [{value, label}], cached once:
    CPU first, then each GPU by index, then MPS when available.
    """
    global _DEVICES
    if _DEVICES is not None:
        return _DEVICES
    devs = [{"value": "-1", "label": "CPU"}]
    if torch is not None:
        try:
            if torch.cuda.is_available():
                for i in range(torch.cuda.device_count()):
                    try:
                        nm = torch.cuda.get_device_name(i)
                    except Exception:
                        nm = f"GPU {i}"
                    devs.append({"value": str(i), "label": f"GPU {i} - {nm}"})
            mps = getattr(torch.backends, "mps", None)
            if mps is not None and mps.is_available() and mps.is_built():
                devs.append({"value": "mps", "label": "MPS (Apple)"})
        except Exception:
            pass
    _DEVICES = devs
    return _DEVICES

def onnx_providers():
    """! @brief ONNX Runtime providers for this backend, preferred first, ending in CPU.
    Filtered to what the installed onnxruntime offers when it imports cheaply.
    Pass this to any InferenceSession instead of hardcoding CUDA.
    """
    b = backend()
    if b == "rocm":
        pref = ["MIGraphXExecutionProvider", "CPUExecutionProvider"]
    elif b == "cuda":
        pref = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    else:
        return ["CPUExecutionProvider"]
    try:
        import onnxruntime as ort
        avail = set(ort.get_available_providers())
        filtered = [p for p in pref if p in avail]
        if "CPUExecutionProvider" not in filtered:
            filtered.append("CPUExecutionProvider")
        return filtered
    except Exception:
        return pref

def onnx_provider():
    """! @brief The single provider name every ONNX model here runs on (first of
    onnx_providers()), for libraries that take one name.
    """
    return onnx_providers()[0]


_ONNX_STD = {"installed": False}


def standardize_onnx(log=None):
    """! @brief Wrap InferenceSession once so a request for a provider this wheel lacks
    (rtmlib asks for ROCm on a MIGraphX build and drops to CPU) gets the app's
    list instead. Satisfiable requests pass untouched; idempotent.
    """
    if _ONNX_STD["installed"]:
        return True
    try:
        import onnxruntime as ort
    except Exception:
        return False
    orig = ort.InferenceSession.__init__
    avail = set(ort.get_available_providers())
    seen = set()

    def _init(self, path_or_bytes, sess_options=None, providers=None, provider_options=None, **kw):
        names = [p if isinstance(p, str) else p[0] for p in (providers or [])]
        missing = [n for n in names if n not in avail]
        if missing:
            std = onnx_providers()
            key = (tuple(names), tuple(std))
            if key not in seen:
                seen.add(key)
                msg = (f"onnx: {', '.join(missing)} not in this onnxruntime build "
                       f"({', '.join(sorted(avail))}); using {std[0]}")
                (log.info if log else lambda m: print(m, file=sys.stderr))(msg)
            providers, provider_options = std, None
        return orig(self, path_or_bytes, sess_options, providers, provider_options, **kw)
    ort.InferenceSession.__init__ = _init
    _ONNX_STD["installed"] = True
    return True


def onnx_device_id():
    """! @brief Integer device id for ONNX-style APIs: 0 on a GPU backend, -1 on CPU."""
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
    """! @brief (rss_mb, vram_mb) before a load."""
    return (_rss_mb(), _vram_mb())

def _measure_cost(before, dev):
    """! @brief Memory a load actually used: the VRAM delta on CUDA, else the RSS delta.
    @return MB, or 0 when the delta is noise (the declared estimate then stays).
    """
    rss0, vram0 = before
    on_cuda = dev is not None and str(dev).lower().startswith(("cuda", "gpu"))
    if on_cuda:
        d = _vram_mb() - vram0
        if d > 1.0:
            return d
        # VRAM delta unreliable here; RSS still moves for host buffers.
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
    """! @brief Thread-safe LRU cache of heavy models. A failed load is cached as None
    and retried once; eviction never touches the key being acquired.
    """

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
                    "model_path": model_path, "seq": 0, "err": ""}
            else:
                new_cost = e["cost_mb"] if e.get("measured") else est
                e.update(loader=loader, unloader=unloader,
                         cost_mb=new_cost, gpu=gpu, model_path=model_path)
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
                        if _file_cost_mb(e.get("model_path")) <= 0:
                            # a first load may download weights
                            from common import wait_for_space
                            wait_for_space(MODELS_DIR)
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
        """! @brief Mark a key most recently used without loading it."""
        with self._lock:
            e = self._entries.get(key)
            if e and e["loaded"]:
                self._seq += 1
                e["seq"] = self._seq

    def hold(self, *keys):
        """! @brief Pin keys resident until release() (refcounted; prefer lease())."""
        with self._lock:
            for k in keys:
                self._leased[k] = self._leased.get(k, 0) + 1

    def release(self, *keys):
        """! @brief Undo one hold() per key, then evict anything over budget."""
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
        """! @brief Free one model (no-op when not loaded)."""
        with self._lock:
            self._unload_locked(key)

    def clear(self, prefix=None):
        """! @brief Free every model, or those whose key starts with `prefix`."""
        with self._lock:
            for k in [k for k in self._entries
                      if prefix is None or str(k).startswith(prefix)]:
                self._unload_locked(k)

    def set_memory_hook(self, hook):
        """! @brief Reserve model memory through the thread manager on load.
        @param hook  fn(cost_mb, gpu) -> context manager with settle(); None disables.
        """
        with self._lock:
            self._mem_hook = hook

    def status(self):
        """! @brief [(key, loaded, cost_mb, gpu)]."""
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
                res.__exit__(None, None, None)  # frees a held VRAM reservation
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