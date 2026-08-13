"""
Automatic tiered storage
========================
Keeps the logical library layout under MEDIA_DIR untouched. A file that has
been "tiered" is replaced by a symlink at its usual rel_path; the bytes live
in an object store on one of the configured tier drives:

    <tier_root>/cim-objects/<aa>/<uuid><ext>

Because every consumer of the library (Flask send_file, cv2, pyexiv2, os.walk,
ffmpeg) transparently follows symlinks, no other code path needs to know
tiering exists. MEDIA_DIR itself (symlinks + SQLite DB + .thumbs cache) should
live on the fastest drive — that automatically satisfies "thumbs on NVMe".

Placement policy
----------------
The user configures N tiers ordered fastest → slowest, each with:
    name        display name ("nvme", "ssd", ...)
    path        directory on that drive
    ratio       share of total library bytes the tier should hold (percent;
                normalised, so 5/50/45 and 1/10/9 mean the same thing)
    speed_mbps  sustained sequential read the drive can actually deliver

Every indexed file gets a byte budget assignment:

  * Videos have a *floor tier*: the slowest tier whose speed_mbps still covers
    bitrate_mbps * video_headroom (default 4x, so seeking/other IO can't cause
    buffering). A 1080p30 ~8 Mbps file happily lands on a 150 MB/s HDD; an
    80 Mbps UHD master is forced up to SSD. Videos *prefer* their floor tier
    (fast flash is wasted on sequential streaming) and are only promoted when
    the floor tier is over budget — highest bitrate promoted first.

  * Images are sorted best-first: higher IQA stars win, ties broken by lower
    bytes-per-pixel (better compressed first). The sorted list fills whatever
    budget the videos left, fastest tier downward. So the best compressed
    images end up on NVMe, the rest on SSD, exactly as budgets allow.

A hysteresis margin (default 5% of a tier's budget) suppresses churn: a file
already sitting in an acceptable tier is not moved just to fix a small
imbalance.

Execution
---------
A daemon thread wakes every `interval_sec`, builds a plan, then executes it
only while the server is idle (same trick as the auto-tagger). Each move:
copy to a temp file on the destination tier → fsync → verify size → atomically
swap the symlink → delete the old copy. Throttled to `throttle_mbps`.
An orphan GC removes tier objects no symlink references any more (covers
deletes/moves that bypassed safe_remove, e.g. cross-device shutil.move).
"""

import os, io, json, time, uuid, shutil, threading, logging

log = logging.getLogger("tiering")
if not log.handlers:
    log.setLevel(logging.INFO)
    try:
        os.makedirs("logs", exist_ok=True)
        _h = logging.FileHandler("logs/tiering.log")
        _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(_h)
    except OSError:
        log.addHandler(logging.StreamHandler())

CFG_FILE   = "tiers_config.json"
OBJECT_DIR = "cim-objects"          # subdir created inside every tier path

DEFAULT_CFG = {
    "enabled": False,
    "tiers": [],                    # [{name,path,ratio,speed_mbps}, ...] fastest first
    "video_headroom": 4.0,          # tier speed must be >= headroom * bitrate
    "hysteresis": 0.05,             # tolerated budget deviation before moving
    "interval_sec": 3600,           # planner cadence
    "throttle_mbps": 200,           # copy bandwidth cap during rebalance
    "idle_sec": 120,                # only move when server idle this long
}

_state = {
    "media_dir": None,
    "db_factory": None,             # returns a sqlite3 connection (thread-local)
    "get_last_activity": None,      # returns epoch seconds of last HTTP request
    "cfg": None,
    "lock": threading.Lock(),
    "run": {                        # progress of the current/last rebalance
        "active": False, "phase": "idle", "planned": 0, "done": 0,
        "moved_bytes": 0, "errors": 0, "last_run": None, "cancel": False,
    },
}

# ── config ────────────────────────────────────────────────────────────────────
def load_cfg():
    cfg = dict(DEFAULT_CFG)
    try:
        with open(CFG_FILE) as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    _state["cfg"] = cfg
    return cfg

def save_cfg(cfg):
    clean = dict(DEFAULT_CFG)
    clean.update({k: cfg[k] for k in cfg if k in DEFAULT_CFG})
    tiers = []
    for t in clean.get("tiers", []):
        try:
            tiers.append({
                "name":       str(t.get("name", "tier")).strip() or "tier",
                "path":       str(t["path"]).strip(),
                "ratio":      max(0.0, float(t.get("ratio", 0))),
                "speed_mbps": max(1.0, float(t.get("speed_mbps", 100))),
            })
        except Exception:
            continue
    clean["tiers"] = [t for t in tiers if t["path"]]
    with open(CFG_FILE, "w") as f:
        json.dump(clean, f, indent=2)
    _state["cfg"] = clean
    return clean

# ── path helpers ──────────────────────────────────────────────────────────────
def _tier_roots(cfg):
    return [os.path.abspath(t["path"]) for t in cfg["tiers"]]

def _object_root(tier_path):
    return os.path.join(os.path.abspath(tier_path), OBJECT_DIR)

def current_tier_of(abs_path, cfg):
    """Index of the tier a library path currently lives on, or None if the
    file is an untiered regular file inside MEDIA_DIR."""
    if not os.path.islink(abs_path):
        return None
    target = os.path.realpath(abs_path)
    for i, root in enumerate(_tier_roots(cfg)):
        if target.startswith(_object_root(root) + os.sep):
            return i
    return None

def safe_remove(path):
    """Drop-in replacement for os.remove that also deletes the tier object a
    symlink points at."""
    if os.path.islink(path):
        target = os.path.realpath(path)
        try:
            os.remove(path)
        finally:
            cfg = _state["cfg"] or load_cfg()
            for root in _tier_roots(cfg):
                if target.startswith(_object_root(root) + os.sep):
                    try: os.remove(target)
                    except FileNotFoundError: pass
                    break
        return
    os.remove(path)

# ── inventory ─────────────────────────────────────────────────────────────────
def _collect_files(db, media_dir, cfg):
    """One record per indexed media file, with size/bitrate/quality info."""
    out = []
    rows = db.execute(
        "SELECT rel_path, media_kind, duration, iqa_score, width, height FROM files"
    ).fetchall()
    for r in rows:
        rel = r["rel_path"]
        ap  = os.path.join(media_dir, rel)
        try:
            # Bill the logical content size, not the inode: a packed file has
            # no disk file, and even before packing os.stat would miss it.
            size = os.stat(ap).st_size
        except OSError:
            continue
        kind = (r["media_kind"] or "image")
        dur  = r["duration"]
        bitrate_mbps = None
        if kind == "video" and dur and dur > 0:
            bitrate_mbps = size * 8 / dur / 1e6
        px = (r["width"] or 0) * (r["height"] or 0)
        out.append({
            "rel": rel, "abs": ap, "size": size, "kind": kind,
            "bitrate": bitrate_mbps,
            "iqa": r["iqa_score"] if r["iqa_score"] is not None else 2.5,
            "bpp": (size / px) if px else 1e9,
            "cur": current_tier_of(ap, cfg),
        })
    return out

# ── planner ───────────────────────────────────────────────────────────────────
def _video_floor_tier(bitrate_mbps, cfg):
    """Slowest tier index that can still stream this video comfortably."""
    tiers = cfg["tiers"]
    need = (bitrate_mbps or 8.0) * cfg["video_headroom"] / 8.0   # MB/s needed
    floor = 0
    for i, t in enumerate(tiers):
        if t["speed_mbps"] >= need:
            floor = i
    # ensure at least one adequate tier; if even tier 0 is too slow, use 0
    for i in range(len(tiers) - 1, -1, -1):
        if tiers[i]["speed_mbps"] >= need:
            return i
    return 0

def plan(db=None):
    """Return (moves, tier_stats). moves = [{rel, from, to, size}]."""
    cfg = _state["cfg"] or load_cfg()
    tiers = cfg["tiers"]
    if not cfg["enabled"] or not tiers:
        return [], []
    db = db or _state["db_factory"]()
    media_dir = _state["media_dir"]

    files = _collect_files(db, media_dir, cfg)
    total = sum(f["size"] for f in files) or 1
    rsum  = sum(t["ratio"] for t in tiers) or 1
    budget = [total * t["ratio"] / rsum for t in tiers]
    used   = [0.0] * len(tiers)         # bytes assigned so far (planned)

    assign = {}                          # rel -> target tier idx

    # 1) videos → floor tier
    videos = [f for f in files if f["kind"] == "video"]
    images = [f for f in files if f["kind"] != "video"]
    for v in videos:
        v["floor"] = _video_floor_tier(v["bitrate"], cfg)
    # place at floor, then promote highest-bitrate videos off over-budget tiers
    by_tier = {}
    for v in videos:
        by_tier.setdefault(v["floor"], []).append(v)
    for i in sorted(by_tier.keys(), reverse=True):          # slowest first
        vs = sorted(by_tier[i], key=lambda v: (v["bitrate"] or 0))
        for v in vs:
            # prefer the floor tier; if it's over budget, promote to the
            # slowest *faster* tier that actually has room. If nothing has
            # room, stay at the floor and overflow there — ratios are soft
            # targets, "won't buffer" is the hard constraint.
            t = i
            if used[t] + v["size"] > budget[t]:
                for cand in range(i - 1, -1, -1):
                    if used[cand] + v["size"] <= budget[cand]:
                        t = cand
                        break
            assign[v["rel"]] = t
            used[t] += v["size"]

    # 2) images best-first into remaining budget, fastest tier downward
    images.sort(key=lambda f: (-f["iqa"], f["bpp"]))
    ti = 0
    for f in images:
        while ti < len(tiers) - 1 and used[ti] + f["size"] > budget[ti]:
            ti += 1
        assign[f["rel"]] = ti
        used[ti] += f["size"]

    # 3) diff against reality, with hysteresis
    hyst = cfg["hysteresis"]
    moves = []
    for f in files:
        tgt, cur = assign[f["rel"]], f["cur"]
        if cur == tgt:
            continue
        # untiered files (cur None) always get placed; tiered files only move
        # if the correction is worth it (their tier is meaningfully off-budget
        # or a video sits on a tier too slow for it)
        if cur is not None:
            too_slow = (f["kind"] == "video" and cur > _video_floor_tier(f["bitrate"], cfg))
            actual_used = _tier_usage_bytes(cur, cfg)
            off_budget = actual_used > budget[cur] * (1 + hyst)
            if not too_slow and not off_budget and abs(cur - tgt) <= 1:
                continue
        moves.append({"rel": f["rel"], "from": cur, "to": tgt, "size": f["size"]})

    stats = [{
        "name": t["name"], "path": t["path"], "ratio": t["ratio"],
        "speed_mbps": t["speed_mbps"],
        "budget_bytes": int(budget[i]),
        "planned_bytes": int(used[i]),
        "actual_bytes": _tier_usage_bytes(i, cfg),
    } for i, t in enumerate(tiers)]
    return moves, stats

def _tier_usage_bytes(idx, cfg):
    root = _object_root(cfg["tiers"][idx]["path"])
    total = 0
    for dirpath, _, names in os.walk(root):
        for n in names:
            try: total += os.stat(os.path.join(dirpath, n)).st_size
            except OSError: pass
    return total

# ── executor ──────────────────────────────────────────────────────────────────
def _dest_object_path(tier_path, rel):
    ext = os.path.splitext(rel)[1].lower()
    name = uuid.uuid4().hex
    d = os.path.join(_object_root(tier_path), name[:2])
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, name + ext)

def _throttled_copy(src, dst, mbps):
    chunk = 4 * 1024 * 1024
    budget_per_sec = max(1.0, mbps) * 1e6
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        t0, sent = time.time(), 0
        while True:
            buf = fi.read(chunk)
            if not buf:
                break
            fo.write(buf)
            sent += len(buf)
            expected = sent / budget_per_sec
            elapsed  = time.time() - t0
            if expected > elapsed:
                time.sleep(expected - elapsed)
        fo.flush(); os.fsync(fo.fileno())
    shutil.copystat(src, dst, follow_symlinks=True)

def _execute_move(mv, cfg):
    media_dir = _state["media_dir"]
    link_path = os.path.join(media_dir, mv["rel"])
    if not os.path.exists(link_path):
        return False
    src_real = os.path.realpath(link_path)
    dst = _dest_object_path(cfg["tiers"][mv["to"]]["path"], mv["rel"])
    tmp = dst + ".part"
    try:
        _throttled_copy(src_real, tmp, cfg["throttle_mbps"])
        if os.stat(tmp).st_size != os.stat(src_real).st_size:
            raise IOError("size mismatch after copy")
        os.replace(tmp, dst)
        # atomic symlink swap: build the new link next to the old one
        ltmp = link_path + f".tierswap-{uuid.uuid4().hex[:8]}"
        os.symlink(dst, ltmp)
        os.replace(ltmp, link_path)          # replaces file OR old symlink
        if src_real != dst and os.path.abspath(src_real) != os.path.abspath(link_path):
            try: os.remove(src_real)
            except OSError: pass
        return True
    except OSError as e:
        log.error(f"move {mv['rel']} -> tier {mv['to']}: {e}")
        for p in (tmp, dst):
            try: os.remove(p)
            except OSError: pass
        return False

def gc_orphans():
    """Delete tier objects that no library symlink references (age > 1h)."""
    cfg = _state["cfg"] or load_cfg()
    media_dir = _state["media_dir"]
    referenced = set()
    for dirpath, dirs, names in os.walk(media_dir):
        for n in names:
            p = os.path.join(dirpath, n)
            if os.path.islink(p):
                referenced.add(os.path.realpath(p))
    removed = 0
    cutoff = time.time() - 3600
    for t in cfg["tiers"]:
        root = _object_root(t["path"])
        for dirpath, _, names in os.walk(root):
            for n in names:
                p = os.path.join(dirpath, n)
                try:
                    if os.path.realpath(p) not in referenced and os.stat(p).st_mtime < cutoff:
                        os.remove(p); removed += 1
                except OSError:
                    pass
    if removed:
        log.info(f"gc: removed {removed} orphaned tier objects")
    return removed

# ── background worker ────────────────────────────────────────────────────────
def _idle():
    cfg = _state["cfg"]
    ga = _state["get_last_activity"]
    return (time.time() - ga()) >= cfg.get("idle_sec", 120) if ga else True

def rebalance(block=False):
    """Kick a rebalance. Returns immediately unless block=True."""
    def work():
        run = _state["run"]
        with _state["lock"]:
            if run["active"]:
                return
            run.update(active=True, phase="planning", planned=0, done=0,
                       moved_bytes=0, errors=0, cancel=False)
        try:
            cfg = load_cfg()
            if not cfg["enabled"] or not cfg["tiers"]:
                run["phase"] = "disabled"; return
            for t in cfg["tiers"]:
                os.makedirs(_object_root(t["path"]), exist_ok=True)
            # verify symlink support once (matters on Windows)
            probe = os.path.join(_object_root(cfg["tiers"][0]["path"]),
                                 ".linktest-" + uuid.uuid4().hex[:8])
            try:
                os.symlink(__file__, probe); os.remove(probe)
            except OSError as e:
                run["phase"] = f"error: symlinks unavailable ({e})"; return
            moves, _ = plan()
            run["planned"] = len(moves)
            run["phase"] = "moving"
            for mv in moves:
                if run["cancel"]:
                    run["phase"] = "cancelled"; break
                while not _idle() and not run["cancel"]:
                    time.sleep(5)
                if _execute_move(mv, cfg):
                    run["done"] += 1
                    run["moved_bytes"] += mv["size"]
                else:
                    run["errors"] += 1
            run["phase"] = "gc"
            gc_orphans()
            if run["phase"] != "cancelled":
                run["phase"] = "idle"
        except Exception as e:
            log.error(f"rebalance failed: {e}")
            run["phase"] = f"error: {e}"
        finally:
            run["active"] = False
            run["last_run"] = time.time()
    if block:
        work()
    else:
        threading.Thread(target=work, daemon=True).start()

def _loop():
    while True:
        cfg = _state["cfg"] or load_cfg()
        time.sleep(max(60, cfg.get("interval_sec", 3600)))
        cfg = load_cfg()
        if cfg["enabled"] and cfg["tiers"] and _idle():
            rebalance(block=True)

def start(media_dir, db_factory, get_last_activity):
    _state["media_dir"] = os.path.abspath(media_dir)
    _state["db_factory"] = db_factory
    _state["get_last_activity"] = get_last_activity
    load_cfg()
    threading.Thread(target=_loop, daemon=True).start()

def status():
    cfg = _state["cfg"] or load_cfg()
    try:
        _, stats = plan()
    except Exception as e:
        stats = []
        log.error(f"status plan failed: {e}")
    return {"config": cfg, "tiers": stats, "run": dict(_state["run"])}