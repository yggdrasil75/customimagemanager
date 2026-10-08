"""! @file
@brief The global thread manager: fair-share worker pools, the background
processor that runs every durable queue, and memory / VRAM admission."""
import os
import time
import threading
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor

try:
    import psutil
    _PROC = psutil.Process()
except Exception:
    psutil = None
    _PROC = None

try:
    import torch
except Exception:
    torch = None

RESERVED_SLOTS = 1
IDLE_SECONDS = 60
MEM_BUDGET_FRAC = 0.8
VRAM_BUDGET_FRAC = 0.85
MODEL_OVERHEAD = 1.05
MODEL_SETTLE_SECONDS = 8.0

def _gpu_kind():
    if torch is None:
        return "none"
    try:
        if not torch.cuda.is_available():
            return "none"
        props = torch.cuda.get_device_properties(0)
        vram = getattr(props, "total_memory", 0) or 0
        name = (getattr(props, "name", "") or "").lower()
        integrated_hint = getattr(props, "is_integrated", None)
        if integrated_hint is True:
            return "shared"
        if any(t in name for t in ("integrated", "igpu", " apu", "radeon graphics",
                                   "iris", "uhd graphics", "hd graphics", "vega")):
            # integrated GPU names (Intel iGPU, AMD APU / Vega)
            return "shared"
        total_ram = 0
        if psutil is not None:
            try:
                total_ram = psutil.virtual_memory().total
            except Exception:
                total_ram = 0
        if total_ram and vram and abs(vram - total_ram) / total_ram < 0.12:
            # its VRAM is system RAM
            return "shared"
        return "dedicated"
    except Exception:
        return "none"

def _detect_mem_limit_mb():
    """! @brief The memory limit this process runs under, in MB (0 = none): the cgroup
    limit (Docker --memory, k8s; v2 or v1), else total RAM. A limit at or above
    total RAM counts as none.
    """
    total = 0
    if psutil is not None:
        try:
            total = psutil.virtual_memory().total
        except Exception:
            total = 0
    limit = 0
    # cgroup v2
    for path in ("/sys/fs/cgroup/memory.max",
                 "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            with open(path) as f:
                raw = f.read().strip()
            if raw and raw != "max":
                limit = int(raw)
                break
        except Exception:
            continue
    # an 'unlimited' cgroup reports a huge sentinel
    if limit and (limit < (total or limit + 1)) and limit < (1 << 62):
        return limit / (1024 * 1024)
    if total:
        return total / (1024 * 1024)
    return 0.0

def _default_max():
    n = os.cpu_count() or 8
    return max(2, n)

_TLS = threading.local()  # .worker: True on a pool job thread


def _nice_worker(nice=10):
    """! @brief Lower this thread's priority (Linux, per thread) so background jobs yield to requests."""
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), nice)
    except Exception:
        pass


class ThreadManager:
    """! @brief The global slot allocator: counts active tasks and hands each a fair share
    of the spare slots; also the memory / VRAM admission and the background
    processor that runs every durable queue.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._active = 0  # live tasks sharing the spares
        self._get_last_activity = None  # set by set_activity_source()
        self._foreground = None
        self._inflight = set()  # running futures, for slot accounting

    def max_slots(self):
        """! @brief Total slots: the CPU count, at least 2."""
        return _default_max()

    def reserved(self):
        """! @brief Slots kept free for responsiveness (at least 1 spare remains)."""
        return max(0, min(RESERVED_SLOTS, self.max_slots() - 1))

    def spare(self):
        """! @brief Slots available to background and feature tasks."""
        return max(1, self.max_slots() - self.reserved())

    def face_batch_size(self):
        kind = self.gpu_kind()
        if kind == "dedicated":
            # Batch size from the card's total VRAM (activations are transient):
            # ~8 GB -> 16, ~16 GB -> 32, ~32 GB -> 64.
            vram_gb = 0.0
            try:
                if torch is not None and torch.cuda.is_available():
                    vram_gb = torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
            except Exception:
                vram_gb = 0.0
            n = int(vram_gb * 2)  # ~2 images per GB
            # power of two, 4..64
            b = 1
            while b * 2 <= n:
                b *= 2
            return max(4, min(64, b))
        if kind == "shared":
            return 4
        # CPU only: one image per core
        return max(1, min(8, (os.cpu_count() or 1)))

    def slots_for(self, want=None):
        """! @brief One task's fair share of the spare slots (at least 1, at most `want`)."""
        with self._lock:
            active = max(1, self._active)
            share = max(1, self.spare() // active)
        if want is not None:
            share = min(share, max(1, int(want)))
        return share

    def _enter(self):
        with self._lock:
            self._active += 1

    def _leave(self):
        with self._lock:
            self._active = max(0, self._active - 1)

    def pool(self, want=None, name=None):
        """! @brief A ThreadPoolExecutor sized to this task's fair share, for a `with` block:

            with tm.pool(want=8) as ex:
                ex.map(...)
        """
        return _ManagedPool(self, want, name)

    def run(self, fn, iterable, want=None, name=None):
        """! @brief executor.map over a fair-share pool. @return results in input order."""
        with self.pool(want=want, name=name) as ex:
            return list(ex.map(fn, iterable))

    ## @brief Register a durable work source for the background processor.
    # One thread round-robins the sources and fills every free pool slot with
    # claim() results; a source never owns threads.
    # @param claim   fn() -> the next job, or None.
    # @param handle  fn(job) runs it.
    # @param key_of  fn(job) -> serialisation key (one job per key at a time).
    # @param cost_of fn(job) -> MB to reserve.
    def register_source(self, name, claim, handle, key_of=None, cost_of=None):
        with self._lock:
            srcs = getattr(self, "_sources", None)
            if srcs is None:
                srcs = self._sources = {}
            srcs[name] = {"claim": claim, "handle": handle,
                          "key_of": key_of, "cost_of": cost_of}
            self._ensure_processor()

    def can_afford(self, cost_mb, inflight_hint=None):
        """! @brief True when a job of `cost_mb` may start now: it fits the headroom, or
        nothing is running (one job always runs, so an oversized one can't deadlock).
        No budget = always True.
        """
        if cost_mb is None or cost_mb <= 0:
            return True
        if self.mem_budget_mb() <= 0:
            return True
        with self._lock:
            running = len({f for f in getattr(self, "_inflight", ()) if not f.done()})
        if (inflight_hint if inflight_hint is not None else running) == 0:
            return True  # one always runs
        return self.mem_headroom_mb() >= cost_mb

    def wake(self):
        """! @brief Wake the background processor now."""
        ev = getattr(self, "_wake", None)
        if ev is not None:
            ev.set()

    def set_foreground(self, name):
        with self._lock:
            self._foreground = name
        self.wake()

    def clear_foreground(self, name=None):
        """! @brief End foreground promotion (only for `name` when given, so a stale clear can't cancel a newer one)."""
        with self._lock:
            if name is None or self._foreground == name:
                self._foreground = None
        self.wake()

    def foreground(self):
        with self._lock:
            return self._foreground

    def _ensure_processor(self):
        # called under _lock; starts the processor once
        if getattr(self, "_proc_started", False):
            return
        self._proc_started = True
        self._wake = threading.Event()
        self._ex = ThreadPoolExecutor(max_workers=self.max_slots(),
                                      thread_name_prefix="bg", initializer=_nice_worker)
        threading.Thread(target=self._process_loop, daemon=True,
                         name="bg-processor").start()

    def _free_slots(self):
        # spare slots minus running jobs
        with self._lock:
            self._inflight = {f for f in self._inflight if not f.done()}
            return self.spare() - len(self._inflight), len(self._inflight)

    def _dispatch(self, src_name, src):
        claim, handle = src["claim"], src["handle"]
        key_of, cost_of = src["key_of"], src["cost_of"]
        try:
            job = claim()
        except Exception:
            return False
        if job is None:
            return False
        key = (key_of(job) if key_of else None) or None
        cost = 0.0
        if cost_of:
            try:
                cost = float(cost_of(job) or 0.0)
            except Exception:
                cost = 0.0
        self._commit_mem(cost)
        def _run(j=job, k=key, c=cost):
            _TLS.worker = True
            try:
                handle(j)
            finally:
                _TLS.worker = False
                self._uncommit_mem(c)
                if k:
                    self.release_key(k)
                self.wake()
        with self._lock:
            try:
                fut = self._ex.submit(_run)
            except RuntimeError:  # executor gone: interpreter exiting
                self._exiting = True
                self._uncommit_mem(cost)
                if key:
                    self.release_key(key)
                return False
            fut._src_name = src_name  # tag the source, for _foreground_idle
            self._inflight.add(fut)
        return True

    def _process_loop(self):
        POLL = 1.0
        while not getattr(self, "_exiting", False):
            try:
                self._process_once(POLL)
            except Exception:
                # A bug in one source must not kill the only background thread: log and go on.
                time.sleep(POLL)

    def _process_once(self, POLL):
        free, _ = self._free_slots()
        if free <= 0:
            self._wake.wait(timeout=POLL); self._wake.clear(); return
        with self._lock:
            all_sources = dict(getattr(self, "_sources", {}))
            fg = self._foreground
        if not all_sources:
            self._wake.wait(timeout=POLL); self._wake.clear(); return

        # A promoted (foreground) source gets the whole pool until it has nothing
        # pending or running; then round-robin resumes.
        if fg is not None:
            src = all_sources.get(fg)
            if src is None:  # promoted source gone
                self.clear_foreground(fg); return
            started = 0
            while free > 0:
                if self._dispatch(fg, src):
                    free -= 1; started += 1
                else:
                    break
            if started == 0:
                if self._foreground_idle(fg):
                    self.clear_foreground(fg)
                else:
                    self._wake.wait(timeout=POLL); self._wake.clear()
            return

        ordered = self._rr_order(all_sources)
        started_any = False
        for i, (name, src) in enumerate(ordered):
            if free <= 0:
                break
            with self._lock:  # a promotion may arrive mid-pass
                if self._foreground is not None:
                    return
            started = 0
            while free > 0:
                if self._dispatch(name, src):
                    free -= 1; started += 1; started_any = True
                else:
                    break
            if started and i == 0 and self._source_idle(name):
                # this source produced work and is drained: the next source leads
                self._advance_rr(name)
            # free slots left: try the next source

        if not started_any:
            self._wake.wait(timeout=POLL); self._wake.clear()

    def _rr_order(self, all_sources):
        """! @brief Sources in service order, rotated so the cursor's source goes first."""
        items = list(all_sources.items())
        cur = getattr(self, "_rr_cursor", None)
        if cur is not None:
            idx = next((i for i, (n, _) in enumerate(items) if n == cur), 0)
            items = items[idx:] + items[:idx]
        return items

    def _advance_rr(self, drained_name):
        """! @brief Move the cursor past a drained source so no source always leads."""
        with self._lock:
            names = list(getattr(self, "_sources", {}).keys())
        if not names:
            return
        try:
            i = names.index(drained_name)
        except ValueError:
            return
        self._rr_cursor = names[(i + 1) % len(names)]

    def inflight(self, name=None):
        """! @brief Jobs running for one source, or all."""
        with self._lock:
            self._inflight = {f for f in self._inflight if not f.done()}
            return sum(1 for f in self._inflight
                       if name is None or getattr(f, "_src_name", None) == name)

    def _source_idle(self, name):
        """! @brief True when a source has no jobs running."""
        with self._lock:
            self._inflight = {f for f in self._inflight if not f.done()}
            return not any(getattr(f, "_src_name", None) == name
                           for f in self._inflight)

    def _foreground_idle(self, name):
        """! @brief True when the foreground source has no jobs running."""
        with self._lock:
            self._inflight = {f for f in self._inflight if not f.done()}
            return not any(getattr(f, "_src_name", None) == name
                           for f in self._inflight)

    def rss_mb(self):
        """! @brief Resident memory in MB, or 0."""
        if _PROC is not None:
            try:
                return _PROC.memory_info().rss / (1024 * 1024)
            except Exception:
                pass
        try:  # /proc fallback
            with open(f"/proc/{os.getpid()}/statm") as f:
                pages = int(f.read().split()[1])
            return pages * (os.sysconf("SC_PAGE_SIZE") / (1024 * 1024))
        except Exception:
            return 0.0

    def mem_budget_mb(self):
        """! @brief Soft RSS budget in MB: CIM_MEM_BUDGET_MB (0 disables), else the cgroup
        limit, else total RAM, times CIM_MEM_BUDGET_FRAC (0.8); 0 when nothing is
        known. Cached briefly.
        """
        now = time.time()
        cached = getattr(self, "_budget_cache", None)
        if cached and now - cached[1] < 10:
            return cached[0]
        limit = _detect_mem_limit_mb()
        budget = int(limit * MEM_BUDGET_FRAC) if limit else 0
        self._budget_cache = (budget, now)
        return budget

    def memory_pressure(self):
        budget = self.mem_budget_mb()
        if budget <= 0:
            return 0.0
        with self._lock:
            committed = getattr(self, "_committed_mb", 0.0)
        return (self.rss_mb() + committed) / budget

    def under_memory_pressure(self, threshold=0.9):
        """! @brief True when RSS is within `threshold` of the budget."""
        return self.memory_pressure() >= threshold

    def ingest_pressure(self):
        """! @brief How loaded the background pool is (for inline vs queued uploads).
        @return {busy, spare, free, mem, saturated}.
        """
        with self._lock:
            inflight = getattr(self, "_inflight", None)
            busy = len({f for f in inflight if not f.done()}) if inflight else 0
        spare = self.spare()
        free  = spare - busy
        mem   = self.memory_pressure()
        return {"busy": busy, "spare": spare, "free": free, "mem": mem,
                "saturated": free <= 0 or mem >= 0.9}

    def mem_headroom_mb(self):
        """! @brief Budget left: budget - RSS - committed job costs (inf without a budget)."""
        budget = self.mem_budget_mb()
        if budget <= 0:
            return float("inf")
        with self._lock:
            committed = getattr(self, "_committed_mb", 0.0)
        return budget - self.rss_mb() - committed

    def _commit_mem(self, cost_mb):
        with self._lock:
            self._committed_mb = getattr(self, "_committed_mb", 0.0) + max(0.0, cost_mb)

    def _uncommit_mem(self, cost_mb):
        with self._lock:
            self._committed_mb = max(0.0,
                getattr(self, "_committed_mb", 0.0) - max(0.0, cost_mb))

    def gpu_kind(self):
        """! @brief "dedicated", "shared" or "none" (cached)."""
        k = getattr(self, "_gpu_kind_cache", None)
        if k is None:
            k = self._gpu_kind_cache = _gpu_kind()
        return k

    def vram_budget_mb(self):
        """! @brief Budget for dedicated VRAM: CIM_VRAM_BUDGET_MB, else a share of the card.
        0 disables (shared GPUs use the RAM budget).
        """
        if self.gpu_kind() != "dedicated":
            return 0
        if torch is None:
            return 0
        try:
            total = torch.cuda.get_device_properties(0).total_memory / (1024 * 1024)
            return int(total * VRAM_BUDGET_FRAC)
        except Exception:
            return 0

    def vram_headroom_mb(self):
        """! @brief VRAM budget left (inf without one)."""
        if self.vram_budget_mb() <= 0:
            return float("inf")
        with self._lock:
            committed = getattr(self, "_committed_vram_mb", 0.0)
        return self.vram_budget_mb() - committed

    def model_cost_target(self, gpu=False, device=None):
        if device is not None:
            d = str(device).lower()
            on_cuda = d.startswith("cuda") or d.startswith("gpu")
            if not on_cuda:
                return "ram"  # CPU, MPS, anything not CUDA
            return "vram" if self.gpu_kind() == "dedicated" else "ram"
        if gpu and self.gpu_kind() == "dedicated":
            return "vram"
        return "ram"

    def model_overhead_factor(self):
        return MODEL_OVERHEAD

    def can_load_model(self, cost_mb, gpu=False, device=None):
        if cost_mb is None or cost_mb <= 0:
            return True
        cost_mb = cost_mb * self.model_overhead_factor()
        if self.model_cost_target(gpu, device) == "vram":
            if self.vram_budget_mb() <= 0:
                return True
            with self._lock:
                committed = getattr(self, "_committed_vram_mb", 0.0)
            if committed <= 0:
                return True
            return self.vram_headroom_mb() >= cost_mb
        # RAM: a CPU model, or a shared GPU
        return self.can_afford(cost_mb)

    def reserve_model(self, cost_mb, gpu=False, device=None):
        padded = float(cost_mb or 0.0) * self.model_overhead_factor()
        target = self.model_cost_target(bool(gpu), device)
        return _ModelReservation(self, padded, target)

    def _commit_vram(self, cost_mb):
        with self._lock:
            self._committed_vram_mb = getattr(self, "_committed_vram_mb", 0.0) + max(0.0, cost_mb)

    def _uncommit_vram(self, cost_mb):
        with self._lock:
            self._committed_vram_mb = max(0.0,
                getattr(self, "_committed_vram_mb", 0.0) - max(0.0, cost_mb))

    def set_activity_source(self, get_last_activity):
        """! @brief Set fn() -> epoch of the last user activity."""
        self._get_last_activity = get_last_activity

    def idle_secs(self):
        return IDLE_SECONDS

    def seconds_since_activity(self):
        if self._get_last_activity is None:
            return float("inf")
        try:
            return max(0.0, time.time() - float(self._get_last_activity()))
        except Exception:
            return float("inf")

    def is_idle(self):
        """! @brief True when the app has been quiet long enough and memory isn't tight."""
        return self.seconds_since_activity() >= self.idle_secs()

    def try_acquire_key(self, key):
        """! @brief Claim a serialisation key (e.g. one download per site) without blocking.
        @return True when claimed (call release_key), False when held elsewhere.
        """
        with self._lock:
            held = getattr(self, "_keys", None)
            if held is None:
                held = self._keys = set()
            if key in held:
                return False
            held.add(key)
            return True

    # Job admission per model (load-time memory is reserve_model's job):
    #   GPU model: each job reserves a working set sized from the model, against
    #              the device budget minus loaded models; gpu_max_jobs is an
    #              optional ceiling (0 = memory alone).
    #   external:  the concurrency its provider declared.
    #   CPU model: not gated here; slots and the RAM budget bound it.
    JOB_MIN_MB = 384  # even a tiny model needs activations
    JOB_FRAC = 0.5  # working set ~ half the model

    def register_model(self, key, *, gpu=False, resource=None, concurrency=1, cost_mb=0):
        """! @brief Sign a model up for job admission (key "<capability>:<provider>").
        @param resource     external backend name; its budget is `concurrency`.
        @param gpu          budget it against device memory; neither = CPU.
        """
        with self._lock:
            models = getattr(self, "_models", None)
            if models is None:
                models = self._models = {}
            models[key] = {"gpu": bool(gpu), "resource": resource,
                           "concurrency": concurrency, "cost_mb": float(cost_mb or 0)}

    def models(self):
        with self._lock:
            return dict(getattr(self, "_models", {}))

    def set_gpu_max_jobs(self, n_or_fn):
        """! @brief Ceiling on concurrent background GPU jobs (int or callable; 0 = memory alone)."""
        self._gpu_max_jobs = n_or_fn

    def gpu_max_jobs(self):
        v = getattr(self, "_gpu_max_jobs", 0)
        try:
            return max(0, int((v() if callable(v) else v) or 0))
        except Exception:
            return 0

    def job_cost_mb(self, key):
        m = self.models().get(key) or {}
        return max(self.JOB_MIN_MB, m.get("cost_mb", 0.0) * self.JOB_FRAC)

    def gpu_job_headroom_mb(self):
        """! @brief Device memory left for jobs (VRAM budget on a card, RAM budget on an APU)."""
        with self._lock:
            jobs = getattr(self, "_committed_gpu_job_mb", 0.0)
            loaded = getattr(self, "_committed_vram_mb", 0.0)
        if self.gpu_kind() == "dedicated":
            budget = self.vram_budget_mb()
            return (budget - loaded - jobs) if budget > 0 else float("inf")
        return self.mem_headroom_mb() - jobs

    def gpu_jobs_running(self):
        with self._lock:
            return len(getattr(self, "_gpu_jobs", {}))

    def try_acquire_model(self, key):
        """! @brief Admit one background job on a model.
        @return a token for release_model() ("" when ungated), or None when the device
                or resource is full or in foreground use.
        """
        m = self.models().get(key) or {}
        if m.get("resource"):
            c = m.get("concurrency", 1)
            try:
                limit = int(c() if callable(c) else c) or 1
            except Exception:
                limit = 1
            return self.try_acquire_slot(m["resource"], limit)
        if not (m.get("gpu") and self.gpu_kind() != "none"):
            return ""
        with self._lock:
            if getattr(self, "_fg_use", {}).get("gpu", 0) > 0:
                return None
            jobs = getattr(self, "_gpu_jobs", None)
            if jobs is None:
                jobs = self._gpu_jobs = {}
            ceiling = self.gpu_max_jobs()
            if ceiling and len(jobs) >= ceiling:
                return None
            if len(jobs) >= self.spare():
                return None
            cost = self.job_cost_mb(key)
            if jobs and self.gpu_job_headroom_mb() < cost:  # one always runs
                return None
            tok = f"gpujob#{getattr(self, '_gpu_job_seq', 0)}"
            self._gpu_job_seq = getattr(self, "_gpu_job_seq", 0) + 1
            jobs[tok] = cost
            self._committed_gpu_job_mb = getattr(self, "_committed_gpu_job_mb", 0.0) + cost
            return tok

    def release_model(self, token):
        """! @brief Return what try_acquire_model handed out."""
        if not token:
            return
        with self._lock:
            jobs = getattr(self, "_gpu_jobs", {})
            if token in jobs:
                self._committed_gpu_job_mb = max(0.0, getattr(self, "_committed_gpu_job_mb", 0.0) - jobs.pop(token))
                self.wake()
                return
        self.release_key(token)

    def try_acquire_slot(self, resource, limit=1):
        """! @brief Claim one of `limit` background slots on a resource.
        @return the key for release_key(), or None when full or in foreground use.
        """
        with self._lock:
            if getattr(self, "_fg_use", {}).get(resource, 0) > 0:
                return None
        for i in range(max(1, int(limit or 1))):
            key = f"{resource}#{i}"
            if self.try_acquire_key(key):
                return key
        return None

    @contextmanager
    def foreground_use(self, resource):
        """! @brief Context manager: hold background claims on `resource` off while it is used interactively."""
        with self._lock:
            fg = getattr(self, "_fg_use", None)
            if fg is None:
                fg = self._fg_use = {}
            fg[resource] = fg.get(resource, 0) + 1
        try:
            yield
        finally:
            with self._lock:
                self._fg_use[resource] = max(0, self._fg_use.get(resource, 1) - 1)
            self.wake()

    def in_worker(self):
        """! @brief True on a pool job thread."""
        return bool(getattr(_TLS, "worker", False))

    def key_free(self, key):
        """! @brief True when `key` is free (peek only)."""
        with self._lock:
            held = getattr(self, "_keys", None)
            return not (held and key in held)

    def release_key(self, key):
        """! @brief Release a key (no-op when not held)."""
        with self._lock:
            held = getattr(self, "_keys", None)
            if held is not None:
                held.discard(key)

    def held_keys(self):
        with self._lock:
            return set(getattr(self, "_keys", None) or ())

    def status(self):
        with self._lock:
            active = self._active
        return {
            "max_slots": self.max_slots(),
            "reserved": self.reserved(),
            "spare": self.spare(),
            "active_tasks": active,
            "slots_each": self.slots_for(),
            "rss_mb": round(self.rss_mb(), 1),
            "mem_budget_mb": self.mem_budget_mb(),
            "mem_headroom_mb": (round(self.mem_headroom_mb(), 1)
                                if self.mem_budget_mb() > 0 else None),
            "committed_mb": round(getattr(self, "_committed_mb", 0.0), 1),
            "memory_pressure": round(self.memory_pressure(), 3),
            "gpu_kind": self.gpu_kind(),
            "vram_budget_mb": self.vram_budget_mb(),
            "vram_headroom_mb": (round(self.vram_headroom_mb(), 1)
                                 if self.vram_budget_mb() > 0 else None),
            "committed_vram_mb": round(getattr(self, "_committed_vram_mb", 0.0), 1),
            "idle": self.is_idle(),
            "seconds_since_activity": round(self.seconds_since_activity(), 1),
            "held_keys": sorted(self.held_keys()),
        }

class _ModelReservation:
    """! @brief Reserve a model's memory for a `with` block.
    VRAM (dedicated card): held for the whole block; those bytes are not in RSS.
    RAM (CPU or shared GPU): covers the load spike, then settles, because RSS
    then counts the model and holding it would count it twice.
    """

    def resize(self, new_cost_mb):
        """! @brief Adjust to the measured cost (plus overhead pad) on the current pool."""
        new_cost = max(0.0, float(new_cost_mb or 0.0)) * self._tm.model_overhead_factor()
        if not self._active:
            self._cost = new_cost
            return
        delta = new_cost - self._cost
        if abs(delta) < 0.5:
            return
        if self._target == "vram":
            self._tm._commit_vram(delta) if delta > 0 else self._tm._uncommit_vram(-delta)
        else:
            self._tm._commit_mem(delta) if delta > 0 else self._tm._uncommit_mem(-delta)
        self._cost = new_cost

    def retarget(self, device):
        new_target = self._tm.model_cost_target(device=device)
        if new_target == self._target:
            return
        if self._active and self._cost > 0:
            # move the commitment across pools
            if self._target == "vram":
                self._tm._uncommit_vram(self._cost)
                self._tm._commit_mem(self._cost)
            else:
                self._tm._uncommit_mem(self._cost)
                self._tm._commit_vram(self._cost)
        self._target = new_target

    def __init__(self, tm, cost_mb, target):
        self._tm = tm
        self._cost = max(0.0, cost_mb)
        self._target = target
        self._active = False

    def __enter__(self):
        if self._cost <= 0:
            return self
        if self._target == "vram":
            self._tm._commit_vram(self._cost)
        else:
            self._tm._commit_mem(self._cost)
        self._active = True
        return self

    def settle(self):
        """! @brief Release a RAM reservation after CIM_MODEL_SETTLE_SECS (8 s): a fresh
        model's memory faults in lazily, so RSS lags behind. No-op for VRAM.
        """
        if not (self._active and self._target == "ram"):
            return
        delay = MODEL_SETTLE_SECONDS
        if delay <= 0:
            self._tm._uncommit_mem(self._cost); self._active = False
            return
        def _release(cost=self._cost):
            self._tm._uncommit_mem(cost)
        self._active = False  # settled; the timer frees the bytes
        t = threading.Timer(delay, _release)
        t.daemon = True
        t.start()

    def __exit__(self, exc_type, exc, tb):
        if self._active:
            if self._target == "vram":
                self._tm._uncommit_vram(self._cost)
            else:
                self._tm._uncommit_mem(self._cost)
            self._active = False
        return False

class _ManagedPool:
    """! @brief What ThreadManager.pool() returns: counts the task and sizes the executor."""

    def __init__(self, tm, want, name):
        self._tm = tm
        self._want = want
        self._name = name
        self._ex = None

    def __enter__(self):
        self._tm._enter()
        workers = self._tm.slots_for(self._want)
        kw = {"max_workers": workers, "initializer": _nice_worker}
        if self._name:
            kw["thread_name_prefix"] = self._name
        self._ex = ThreadPoolExecutor(**kw)
        return self._ex

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._ex is not None:
                self._ex.shutdown(wait=True)
        finally:
            self._tm._leave()
        return False

# process-wide manager and module-level shortcuts
MANAGER = ThreadManager()

max_slots = MANAGER.max_slots
spare = MANAGER.spare
slots_for = MANAGER.slots_for
pool = MANAGER.pool
run = MANAGER.run
register_source = MANAGER.register_source
wake = MANAGER.wake
set_foreground = MANAGER.set_foreground
clear_foreground = MANAGER.clear_foreground
foreground = MANAGER.foreground
rss_mb = MANAGER.rss_mb
mem_budget_mb = MANAGER.mem_budget_mb
mem_headroom_mb = MANAGER.mem_headroom_mb
can_afford = MANAGER.can_afford
gpu_kind = MANAGER.gpu_kind
vram_budget_mb = MANAGER.vram_budget_mb
vram_headroom_mb = MANAGER.vram_headroom_mb
can_load_model = MANAGER.can_load_model
reserve_model = MANAGER.reserve_model
memory_pressure = MANAGER.memory_pressure
under_memory_pressure = MANAGER.under_memory_pressure
set_activity_source = MANAGER.set_activity_source
is_idle = MANAGER.is_idle
try_acquire_key = MANAGER.try_acquire_key
key_free = MANAGER.key_free
release_key = MANAGER.release_key
held_keys = MANAGER.held_keys
seconds_since_activity = MANAGER.seconds_since_activity
status = MANAGER.status
register_model = MANAGER.register_model
models = MANAGER.models
try_acquire_model = MANAGER.try_acquire_model
release_model = MANAGER.release_model
set_gpu_max_jobs = MANAGER.set_gpu_max_jobs
gpu_max_jobs = MANAGER.gpu_max_jobs
gpu_jobs_running = MANAGER.gpu_jobs_running
job_cost_mb = MANAGER.job_cost_mb
gpu_job_headroom_mb = MANAGER.gpu_job_headroom_mb
try_acquire_slot = MANAGER.try_acquire_slot
foreground_use = MANAGER.foreground_use
in_worker = MANAGER.in_worker