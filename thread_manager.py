import os
import time
import threading
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
            # Common integrated-GPU name fragments (Intel iGPU, AMD APU/Vega).
            return "shared"
        total_ram = 0
        if psutil is not None:
            try:
                total_ram = psutil.virtual_memory().total
            except Exception:
                total_ram = 0
        if total_ram and vram and abs(vram - total_ram) / total_ram < 0.12:
            # Its 'VRAM' is essentially the machine's RAM -> shared.
            return "shared"
        return "dedicated"
    except Exception:
        return "none"

def _detect_mem_limit_mb():
    """The memory limit this process actually runs under, in MB, or 0 if none.

    Prefers the cgroup limit (what a Docker `--memory` cap or a k8s limit sets),
    which is the number that matters on Docker-for-Windows/Mac where the whole
    engine runs in a memory-capped VM. Falls back to total system RAM. Reads both
    cgroup v2 (memory.max) and v1 (memory.limit_in_bytes); a limit at/above total
    RAM means 'unlimited' and is ignored."""
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
    # A cgroup 'limit' of ~unlimited shows as a huge sentinel; treat >= total RAM
    # (or absurdly large) as no real cap.
    if limit and (limit < (total or limit + 1)) and limit < (1 << 62):
        return limit / (1024 * 1024)
    if total:
        return total / (1024 * 1024)
    return 0.0

def _default_max():
    n = os.cpu_count() or 8
    return max(2, n)

class ThreadManager:
    """Global slot allocator. Thread-safe. Not a fixed executor: it tracks how
    many logical tasks are active and computes each task's fair share of the
    spare (non-reserved) slots on demand."""

    def __init__(self):
        self._lock = threading.RLock()
        self._active = 0          # number of live logical tasks sharing spares
        self._get_last_activity = None   # set via set_activity_source()

    # ── sizing ────────────────────────────────────────────────────────────
    def max_slots(self):
        """Total worker slots in the global pool: the CPU count (min 2)."""
        return _default_max()

    def reserved(self):
        """Slots kept free for responsiveness; clamped so at least 1 spare
        remains."""
        return max(0, min(RESERVED_SLOTS, self.max_slots() - 1))

    def spare(self):
        """Slots available to hand out to background/feature tasks."""
        return max(1, self.max_slots() - self.reserved())

    def slots_for(self, want=None):
        """Fair share of the spare pool for one task, given how many tasks are
        currently active. Always >= 1; never more than `want` if given."""
        with self._lock:
            active = max(1, self._active)
            share = max(1, self.spare() // active)
        if want is not None:
            share = min(share, max(1, int(want)))
        return share

    # ── task lifecycle ────────────────────────────────────────────────────
    def _enter(self):
        with self._lock:
            self._active += 1

    def _leave(self):
        with self._lock:
            self._active = max(0, self._active - 1)

    def pool(self, want=None, name=None):
        """Context manager yielding a ThreadPoolExecutor sized to this task's
        fair share. Registers the task for the duration so concurrent tasks
        split the spares evenly.

            with tm.pool(want=8) as ex:
                ex.map(...)
        """
        return _ManagedPool(self, want, name)

    def run(self, fn, iterable, want=None, name=None):
        """Convenience: map fn over iterable on a fair-share pool, return list of
        results in completion order is NOT guaranteed — order follows executor.map
        (input order)."""
        with self.pool(want=want, name=name) as ex:
            return list(ex.map(fn, iterable))

    # ── background processor ──────────────────────────────────────────────
    # One worker thread services every durable queue (upload, gdl, …). Each
    # subsystem registers a *source*: a claim() that returns the next unit of
    # work or None, and a handle() that runs it. The loop round-robins the
    # sources, and on each pass fills every free thread in the shared pool with
    # whatever it can claim — so a subsystem never owns threads or sizes a pool,
    # it just says "here's how to get one job and how to run it." Concurrency is
    # the global spare-slot budget, shared across all sources at once.
    def register_source(self, name, claim, handle, key_of=None, cost_of=None):
        with self._lock:
            srcs = getattr(self, "_sources", None)
            if srcs is None:
                srcs = self._sources = {}
            srcs[name] = {"claim": claim, "handle": handle,
                          "key_of": key_of, "cost_of": cost_of}
            self._ensure_processor()

    def can_afford(self, cost_mb, inflight_hint=None):
        """True if a job estimated at cost_mb MB may start now: either it fits in
        current headroom, or nothing is running yet (we always let at least one
        job through so a job bigger than the whole budget can't deadlock the
        queue — the best a tiny box can do is one-at-a-time). No budget set →
        always True. Sources call this in claim() before taking a big row."""
        if cost_mb is None or cost_mb <= 0:
            return True
        if self.mem_budget_mb() <= 0:
            return True
        with self._lock:
            running = len({f for f in getattr(self, "_inflight", ()) if not f.done()})
        if (inflight_hint if inflight_hint is not None else running) == 0:
            return True                      # deadlock guard: one always runs
        return self.mem_headroom_mb() >= cost_mb

    def wake(self):
        """Nudge the background processor to look for work now."""
        ev = getattr(self, "_wake", None)
        if ev is not None:
            ev.set()

    def _ensure_processor(self):
        # Called under _lock. Starts the single processor thread once.
        if getattr(self, "_proc_started", False):
            return
        self._proc_started = True
        self._wake = threading.Event()
        self._inflight = set()          # live futures, for slot accounting
        self._ex = ThreadPoolExecutor(max_workers=self.max_slots(),
                                      thread_name_prefix="bg")
        threading.Thread(target=self._process_loop, daemon=True,
                         name="bg-processor").start()

    def _free_slots(self):
        # Spare budget minus jobs currently running under the processor.
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
            try:
                handle(j)
            finally:
                self._uncommit_mem(c)
                if k:
                    self.release_key(k)
                self.wake()
        with self._lock:
            self._inflight.add(self._ex.submit(_run))
        return True

    def _process_loop(self):
        POLL = 1.0
        while True:
            free, _ = self._free_slots()
            if free <= 0:
                self._wake.wait(timeout=POLL); self._wake.clear(); continue
            with self._lock:
                sources = list(getattr(self, "_sources", {}).items())
            if not sources:
                self._wake.wait(timeout=POLL); self._wake.clear(); continue
            # Round-robin: one pass tries each source in turn, filling free
            # threads. Keep passing while jobs are still being started and slots
            # remain; stop when a full pass starts nothing (everything idle/busy).
            started_any = False
            while free > 0:
                progressed = False
                for name, src in sources:
                    if free <= 0:
                        break
                    if self._dispatch(name, src):
                        free -= 1
                        progressed = True
                        started_any = True
                if not progressed:
                    break
            if not started_any:
                self._wake.wait(timeout=POLL); self._wake.clear()

    # ── memory manager ────────────────────────────────────────────────────
    def rss_mb(self):
        """Resident set size in MB, or 0 if unknown."""
        if _PROC is not None:
            try:
                return _PROC.memory_info().rss / (1024 * 1024)
            except Exception:
                pass
        try:  # /proc fallback (Linux)
            with open(f"/proc/{os.getpid()}/statm") as f:
                pages = int(f.read().split()[1])
            return pages * (os.sysconf("SC_PAGE_SIZE") / (1024 * 1024))
        except Exception:
            return 0.0

    def mem_budget_mb(self):
        """Soft RSS budget in MB. Resolution order:
          1. CIM_MEM_BUDGET_MB if set (explicit wins; 0 disables admission).
          2. The container's cgroup memory limit (Docker on Windows/Mac/Linux all
             surface one) times CIM_MEM_BUDGET_FRAC, default 0.8 — this is what
             makes a 2 GB Docker cap Just Work with no configuration.
          3. Total system RAM times the same fraction, if psutil is present.
          4. 0 (disabled) when nothing is detectable.
        Cached briefly since cgroup reads hit the filesystem."""
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
        """True when RSS is within `threshold` of the soft budget. Producers can
        poll this to shrink batches or pause enqueuing."""
        return self.memory_pressure() >= threshold

    def mem_headroom_mb(self):
        """MB of soft budget still free right now (budget - current RSS -
        committed cost of in-flight jobs). inf when no budget is set."""
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

    # ── GPU / VRAM axis ───────────────────────────────────────────────────
    def gpu_kind(self):
        """'dedicated', 'shared', or 'none' — cached. See _gpu_kind."""
        k = getattr(self, "_gpu_kind_cache", None)
        if k is None:
            k = self._gpu_kind_cache = _gpu_kind()
        return k

    def vram_budget_mb(self):
        """Soft budget for DEDICATED VRAM (its own pool, separate from RAM). 0
        disables the axis. Explicit CIM_VRAM_BUDGET_MB wins; else a fraction of
        the discrete card's reported memory. Meaningless on shared/none GPUs
        (their memory is RAM and is governed by the RAM budget instead)."""
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
        """Free dedicated-VRAM budget (budget - committed model VRAM). inf when no
        VRAM budget/axis."""
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
                return "ram"                       # cpu / mps / anything non-CUDA
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
        # RAM path (CPU model, or shared/integrated GPU whose VRAM is system RAM).
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

    # ── idle tool ─────────────────────────────────────────────────────────
    def set_activity_source(self, get_last_activity):
        """Register a zero-arg callable returning the epoch time of the last
        user activity (manager passes `lambda: _last_activity`)."""
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
        """True when the app has been quiet long enough for background work.
        Also refuses to report idle while under memory pressure, so heavy
        background jobs don't kick off when there's no headroom."""
        if self.under_memory_pressure():
            return False
        return self.seconds_since_activity() >= self.idle_secs()

    # ── per-key serialization gate ────────────────────────────────────────
    def try_acquire_key(self, key):
        """Non-blocking claim of a serialization key (e.g. a gdl site). Returns
        True if this caller now owns `key` and must call release_key(key) when
        done; False if another task already holds it. Used to guarantee at most
        one in-flight task per key (one download per site) while different keys
        run concurrently."""
        with self._lock:
            held = getattr(self, "_keys", None)
            if held is None:
                held = self._keys = set()
            if key in held:
                return False
            held.add(key)
            return True

    def release_key(self, key):
        """Release a key claimed via try_acquire_key. Safe if not held."""
        with self._lock:
            held = getattr(self, "_keys", None)
            if held is not None:
                held.discard(key)

    def held_keys(self):
        with self._lock:
            return set(getattr(self, "_keys", None) or ())

    # ── introspection ─────────────────────────────────────────────────────
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
    """Reserve a model's memory against the right pool for the duration of a
    `with` block.

    The RAM and VRAM cases are asymmetric on purpose:

    * RAM (CPU model, or a shared / integrated GPU whose 'VRAM' is system RAM):
      once the model is loaded its bytes are already in the process RSS, and
      mem_headroom_mb subtracts RSS. Holding the reservation for the model's
      whole life would double-count it. So the RAM reservation is a *transient*
      that covers the load spike, then settles: after `settle()` (or a short
      grace once entered) the committed amount is released because RSS now
      reflects it. Callers that just wrap acquire+use get the load covered; the
      steady state is accounted by RSS.

    * VRAM (dedicated GPU): those bytes are NOT in RSS — they live on the card.
      The reservation is held for the entire `with` block so vram_headroom_mb
      reflects the pinned model the whole time.

    Either budget being disabled makes the accounting a no-op."""

    def resize(self, new_cost_mb):
        """Adjust a live reservation to the measured cost (the declared estimate
        was only a guess). Applies the overhead pad and commits/releases the
        delta against the current target pool."""
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
            # Move the live commitment across pools.
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
        """Begin releasing a RAM reservation, but on a grace delay rather than
        instantly. A just-loaded model's bytes aren't all in RSS yet (allocation
        is lazy — memory faults in over the first moments of use), so dropping the
        reservation the instant load() returns would open a window where headroom
        looks larger than it really is and an upload raw could slip in and OOM.
        We keep the reservation for CIM_MODEL_SETTLE_SECS (default 8s) so RSS
        catches up, then release — after which the model is accounted purely by
        RSS. No-op for VRAM (never in RSS; held until unload)."""
        if not (self._active and self._target == "ram"):
            return
        delay = MODEL_SETTLE_SECONDS
        if delay <= 0:
            self._tm._uncommit_mem(self._cost); self._active = False
            return
        def _release(cost=self._cost):
            self._tm._uncommit_mem(cost)
        self._active = False           # logically settled; timer frees the bytes
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
    """Context manager returned by ThreadManager.pool(). Counts the task as
    active for its lifetime and builds a right-sized ThreadPoolExecutor."""

    def __init__(self, tm, want, name):
        self._tm = tm
        self._want = want
        self._name = name
        self._ex = None

    def __enter__(self):
        self._tm._enter()
        workers = self._tm.slots_for(self._want)
        kw = {"max_workers": workers}
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

# Process-wide singleton + module-level shortcuts (mirrors model_registry).
MANAGER = ThreadManager()

max_slots = MANAGER.max_slots
spare = MANAGER.spare
slots_for = MANAGER.slots_for
pool = MANAGER.pool
run = MANAGER.run
register_source = MANAGER.register_source
wake = MANAGER.wake
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
release_key = MANAGER.release_key
held_keys = MANAGER.held_keys
seconds_since_activity = MANAGER.seconds_since_activity
status = MANAGER.status