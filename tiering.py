"""! @file
@brief Tiered storage: library files move between drives by speed and value.

A tiered file is a symlink at its usual rel_path pointing at
<tier>/cim-objects/<aa>/<DocumentID><ext>; everything that opens it follows
the link. Each tier has a path, a share of the library (ratio) and a read
speed. Videos sit on the slowest tier fast enough for their bitrate times
`video_headroom`, promoted only when that tier is over budget. Images fill the
remaining budget best first (IQA stars, then bytes per pixel). A hysteresis
margin stops small imbalances from moving files.

The sidecar keeps the file's DocumentID, which names the object, so lost
symlinks are rebuilt by restore_orphans(). Moves run in a daemon thread while
the server is idle: copy, fsync, verify, swap the symlink, delete the old copy,
throttled to `throttle_mbps`. gc_orphans() removes objects nothing points at.
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
OBJECT_DIR = "cim-objects"

DEFAULT_CFG = {
    "enabled": False,
    "tiers": [],  # [{name, path, ratio, speed_mbps}], fastest first
    "video_headroom": 4.0,  # tier MB/s must cover bitrate x headroom
    "hysteresis": 0.05,  # budget deviation tolerated before moving
    "interval_sec": 3600,
    "throttle_mbps": 200,  # copy bandwidth while rebalancing
    "idle_sec": 120,  # seconds of HTTP silence before moving
}

_state = {
    "media_dir": None,
    "db_factory": None,  # fn() -> sqlite3 connection
    "get_last_activity": None,  # fn() -> epoch of the last HTTP request
    "load_stored_cfg": None,  # fn() -> saved config or None
    "store_cfg": None,  # fn(cfg): save config
    "cfg": None,
    "lock": threading.Lock(),
    "run": {  # progress of the current / last rebalance
        "active": False, "phase": "idle", "planned": 0, "done": 0,
        "moved_bytes": 0, "errors": 0, "last_run": None, "cancel": False,
    },
}

def _read_legacy_file():
    """! @brief The legacy tiers_config.json, read once to migrate it."""
    try:
        with open(CFG_FILE) as f:
            return json.load(f)
    except Exception:
        return None

def load_cfg():
    """! @brief Tier config from app_config.json; a legacy file is migrated on first read."""
    cfg = dict(DEFAULT_CFG)
    stored = None
    loader = _state.get("load_stored_cfg")
    if loader:
        try:
            stored = loader()
        except Exception:
            stored = None
    migrated_from_legacy = False
    if not stored:
        legacy = _read_legacy_file()
        if legacy:
            stored = legacy
            migrated_from_legacy = True
    if stored:
        cfg.update({k: stored[k] for k in stored if k in DEFAULT_CFG})
    _state["cfg"] = cfg
    if migrated_from_legacy and _state.get("store_cfg"):
        try:
            _state["store_cfg"](cfg)
        except Exception:
            pass
    return cfg

def _sanitize_cfg(cfg):
    """! @brief Coerce a config dict to the known schema."""
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
    return clean

def save_cfg(cfg):
    """! @brief Validate and save the tier config (legacy file only when no host store is wired)."""
    clean = _sanitize_cfg(cfg)
    _state["cfg"] = clean
    storer = _state.get("store_cfg")
    if storer:
        storer(clean)
    else:
        with open(CFG_FILE, "w") as f:
            json.dump(clean, f, indent=2)
    return clean

def _tier_roots(cfg):
    return [os.path.abspath(t["path"]) for t in cfg["tiers"]]

def _object_root(tier_path):
    return os.path.join(os.path.abspath(tier_path), OBJECT_DIR)

def current_tier_of(abs_path, cfg):
    """! @brief Index of the tier holding a library path, or None for an untiered file."""
    if not os.path.islink(abs_path):
        return None
    target = os.path.realpath(abs_path)
    for i, root in enumerate(_tier_roots(cfg)):
        if target.startswith(_object_root(root) + os.sep):
            return i
    return None

def safe_remove(path):
    """! @brief os.remove that also deletes the tier object a symlink points at."""
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

def _collect_files(db, media_dir, cfg):
    """! @brief One record per indexed media file: size, bitrate, quality."""
    out = []
    rows = db.execute(
        "SELECT rel_path, media_kind, duration, iqa_score, width, height FROM files"
    ).fetchall()
    for r in rows:
        rel = r["rel_path"]
        ap  = os.path.join(media_dir, rel)
        try:
            # Bill the logical size: the symlink's own inode is not the file.
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

def _video_floor_tier(bitrate_mbps, cfg):
    """! @brief The slowest tier fast enough to stream this video."""
    tiers = cfg["tiers"]
    need = (bitrate_mbps or 8.0) * cfg["video_headroom"] / 8.0  # MB/s
    floor = 0
    for i, t in enumerate(tiers):
        if t["speed_mbps"] >= need:
            floor = i
    # if even the fastest tier is too slow, use it anyway
    for i in range(len(tiers) - 1, -1, -1):
        if tiers[i]["speed_mbps"] >= need:
            return i
    return 0

def plan(db=None, aggressive=False):
    """! @brief Plan moves.
    @param aggressive  ignore hysteresis: every file off its target tier moves.
    @return (moves [{rel, from, to, size}], tier stats).
    """
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
    used   = [0.0] * len(tiers)  # bytes planned per tier

    assign = {}  # rel -> target tier

    # 1) videos to their floor tier
    videos = [f for f in files if f["kind"] == "video"]
    images = [f for f in files if f["kind"] != "video"]
    for v in videos:
        v["floor"] = _video_floor_tier(v["bitrate"], cfg)
    by_tier = {}
    for v in videos:
        by_tier.setdefault(v["floor"], []).append(v)
    for i in sorted(by_tier.keys(), reverse=True):  # slowest first
        vs = sorted(by_tier[i], key=lambda v: (v["bitrate"] or 0))
        for v in vs:
            # Over budget: promote to the slowest faster tier with room, else overflow
            # at the floor. Ratios are soft; "won't buffer" is hard.
            t = i
            if used[t] + v["size"] > budget[t]:
                for cand in range(i - 1, -1, -1):
                    if used[cand] + v["size"] <= budget[cand]:
                        t = cand
                        break
            assign[v["rel"]] = t
            used[t] += v["size"]

    # 2) images best first into the remaining budget
    images.sort(key=lambda f: (-f["iqa"], f["bpp"]))
    ti = 0
    for f in images:
        while ti < len(tiers) - 1 and used[ti] + f["size"] > budget[ti]:
            ti += 1
        assign[f["rel"]] = ti
        used[ti] += f["size"]

    # 3) compare with where files are, with hysteresis
    hyst = cfg["hysteresis"]
    moves = []
    for f in files:
        tgt, cur = assign[f["rel"]], f["cur"]
        if cur == tgt:
            continue
        # An untiered file is always placed; a tiered one moves only when its tier is
        # meaningfully off budget or too slow for it.
        if cur is not None and not aggressive:
            too_slow = (f["kind"] == "video" and cur > _video_floor_tier(f["bitrate"], cfg))
            actual_used = _tier_usage_bytes(cur, cfg)
            off_budget = actual_used > budget[cur] * (1 + hyst)
            if not too_slow and not off_budget and abs(cur - tgt) <= 1:
                continue
        moves.append({"rel": f["rel"], "from": cur, "to": tgt, "size": f["size"]})

    stats = []
    for i, t in enumerate(tiers):
        files_n, actual = _walk_usage(_object_root(t["path"]))
        stats.append({
            "name": t["name"], "path": t["path"], "ratio": t["ratio"],
            "speed_mbps": t["speed_mbps"],
            "budget_bytes": int(budget[i]),
            "planned_bytes": int(used[i]),
            "actual_bytes": actual,
            "actual_files": files_n,
        })
    return moves, stats

def _tier_usage_bytes(idx, cfg):
    return _walk_usage(_object_root(cfg["tiers"][idx]["path"]))[1]

def _walk_usage(root):
    """! @brief (file count, bytes) under root; (0, 0) when missing."""
    count = total = 0
    for dirpath, _, names in os.walk(root):
        for n in names:
            try:
                total += os.stat(os.path.join(dirpath, n)).st_size
                count += 1
            except OSError:
                pass
    return count, total

def _media_usage(cfg):
    """! @brief Files and bytes stored directly in media/ (tier roots inside it excluded)."""
    media_dir = _state["media_dir"]
    if not media_dir:
        return 0, 0
    tier_roots = {os.path.abspath(_object_root(t["path"])) for t in cfg["tiers"]}
    count = total = 0
    for dirpath, dirs, names in os.walk(media_dir):
        if os.path.abspath(dirpath) in tier_roots:
            dirs[:] = []
            continue
        for n in names:
            p = os.path.join(dirpath, n)
            try:
                # symlinks into tiers cost nothing here
                st = os.lstat(p)
                if not os.path.islink(p):
                    total += st.st_size
                    count += 1
            except OSError:
                pass
    return count, total

def _key(doc_id):
    """! @brief A DocumentID in comparable form: alphanumerics, lower case ("xmp.did:AB-12" == "ab12")."""
    return "".join(ch for ch in str(doc_id or "") if ch.isalnum()).lower()

def _dest_object_path(tier_path, rel):
    """! @brief The object path for a library file, named by its DocumentID."""
    ext = os.path.splitext(rel)[1].lower()
    ensure = _state.get("ensure_document_id")
    name = _key(ensure(os.path.join(_state["media_dir"], rel))) if ensure else ""
    name = name or uuid.uuid4().hex
    d = os.path.join(_object_root(tier_path), name[:2])
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, name + ext)

def is_object_path(rel):
    """! @brief True when a library rel_path is inside an object store (a misplaced tier dir)."""
    return str(rel or "").replace("\\", "/").lstrip("/").startswith(OBJECT_DIR + "/")

def _store_objects(cfg):
    """! @brief Every object file in every tier store (temp files excluded)."""
    for t in cfg["tiers"]:
        root = _object_root(t["path"])
        for dirpath, _, names in os.walk(root):
            for n in names:
                if n.endswith(".part") or n.startswith("."):
                    continue
                yield os.path.join(dirpath, n)

def _referenced_objects(media_dir):
    """! @brief {realpath(object): rel_path} for every symlink in the library."""
    out = {}
    for dirpath, dirs, names in os.walk(media_dir):
        dirs[:] = [d for d in dirs if d != OBJECT_DIR and not d.startswith(".")]
        for n in names:
            p = os.path.join(dirpath, n)
            if os.path.islink(p):
                out[os.path.realpath(p)] = os.path.relpath(p, media_dir).replace("\\", "/")
    return out

def _homeless_sidecars(media_dir):
    """! @brief {DocumentID key: sidecar stem} for sidecars whose media is missing."""
    read_id = _state.get("read_document_id")
    if not read_id:
        return {}
    out = {}
    for stem in homeless_media(media_dir):
        k = _key(read_id(os.path.join(media_dir, stem + ".xmp")))
        if k:
            out[k] = stem
    return out

def adopt_ids(cfg=None):
    """! @brief Make object names and DocumentIDs agree for objects stored before ids:
    the sidecar takes the object's uuid, or the object is renamed to the id.
    @return how many were fixed.
    """
    cfg = cfg or _state["cfg"] or load_cfg()
    read_id, ensure = _state.get("read_document_id"), _state.get("ensure_document_id")
    if not (read_id and ensure):
        return 0
    media_dir = _state["media_dir"]
    n = 0
    for obj, rel in _referenced_objects(media_dir).items():
        if not any(obj.startswith(_object_root(r) + os.sep) for r in _tier_roots(cfg)):
            continue
        stem, ext = os.path.splitext(os.path.basename(obj))
        link = os.path.join(media_dir, rel)
        have = _key(read_id(link))
        try:
            if not have:
                ensure(link, stem)
                n += 1
            elif have != stem:
                new = os.path.join(os.path.dirname(os.path.dirname(obj)), have[:2], have + ext)
                os.makedirs(os.path.dirname(new), exist_ok=True)
                os.rename(obj, new)
                ltmp = link + f".tierswap-{uuid.uuid4().hex[:8]}"
                os.symlink(new, ltmp); os.replace(ltmp, link)
                n += 1
        except OSError as e:
            log.error(f"adopt id for {rel}: {e}")
    if n:
        log.info(f"adopt: aligned {n} tier objects with their sidecar DocumentID")
    return n

def unidentified_objects(cfg=None):
    """! @brief Objects no symlink and no sidecar claim. @return [(path, ext)]."""
    cfg = cfg or _state["cfg"] or load_cfg()
    media_dir = _state["media_dir"]
    referenced = _referenced_objects(media_dir)
    homes = _homeless_sidecars(media_dir)
    return [p for p in _store_objects(cfg)
            if os.path.realpath(p) not in referenced
            and _key(os.path.splitext(os.path.basename(p))[0]) not in homes]

def homeless_media(media_dir=None):
    """! @brief Stems of sidecars whose media is missing (where lost objects belong)."""
    media_dir = media_dir or _state["media_dir"]
    out = []
    for dirpath, dirs, names in os.walk(media_dir):
        dirs[:] = [d for d in dirs if d != OBJECT_DIR and not d.startswith(".")]
        for n in names:
            if n.lower().endswith(".xmp") and not any(
                    os.path.exists(os.path.join(dirpath, m)) for m in names
                    if m != n and os.path.splitext(m)[0] == n[:-4]):
                out.append(os.path.relpath(os.path.join(dirpath, n[:-4]), media_dir).replace("\\", "/"))
    return out

def restore_orphans(cfg=None):
    """! @brief Relink every unreferenced object at its sidecar's rel_path.
    @return the restored rel_paths.
    """
    cfg = cfg or _state["cfg"] or load_cfg()
    media_dir = _state["media_dir"]
    referenced = _referenced_objects(media_dir)
    homes = None
    restored = []
    for obj in _store_objects(cfg):
        if os.path.realpath(obj) in referenced:
            continue
        if homes is None:
            homes = _homeless_sidecars(media_dir)
        stem, ext = os.path.splitext(os.path.basename(obj))
        rel_stem = homes.get(_key(stem))
        if not rel_stem:
            continue
        rel = rel_stem + ext
        link = os.path.join(media_dir, rel)
        if os.path.lexists(link):
            if os.path.islink(link) and not os.path.exists(link):
                os.remove(link)  # dangling link
            else:
                continue
        try:
            os.symlink(obj, link)
            referenced[os.path.realpath(obj)] = rel
            restored.append(rel)
        except OSError as e:
            log.error(f"restore {rel} -> {obj}: {e}")
    if restored:
        log.info(f"restore: relinked {len(restored)} tier objects at their library paths")
    return restored

def _throttled_copy(src, dst, mbps):
    chunk = 4 * 1024 * 1024
    budget_per_sec = max(1.0, mbps) * 1e6 if mbps else float("inf")
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

def _execute_move(mv, cfg, mbps=None):
    media_dir = _state["media_dir"]
    link_path = os.path.join(media_dir, mv["rel"])
    if not os.path.exists(link_path):
        return False
    src_real = os.path.realpath(link_path)
    dst = _dest_object_path(cfg["tiers"][mv["to"]]["path"], mv["rel"])
    tmp = dst + ".part"
    try:
        _throttled_copy(src_real, tmp, cfg["throttle_mbps"] if mbps is None else mbps)
        if os.stat(tmp).st_size != os.stat(src_real).st_size:
            raise IOError("size mismatch after copy")
        os.replace(tmp, dst)
        # atomic swap: build the new link beside the old one
        ltmp = link_path + f".tierswap-{uuid.uuid4().hex[:8]}"
        os.symlink(dst, ltmp)
        os.replace(ltmp, link_path)
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
    """! @brief Delete unreferenced tier objects older than an hour."""
    cfg = _state["cfg"] or load_cfg()
    media_dir = _state["media_dir"]
    referenced = _referenced_objects(media_dir)
    homes = _homeless_sidecars(media_dir)
    removed = 0
    cutoff = time.time() - 3600
    for p in _store_objects(cfg):
        try:
            if os.path.realpath(p) in referenced or os.stat(p).st_mtime >= cutoff:
                continue
            # An object with a sidecar was lost, not deleted: restore_orphans relinks it.
            if _key(os.path.splitext(os.path.basename(p))[0]) in homes:
                continue
            os.remove(p); removed += 1
        except OSError:
            pass
    if removed:
        log.info(f"gc: removed {removed} orphaned tier objects")
    return removed

def _idle():
    cfg = _state["cfg"]
    ga = _state["get_last_activity"]
    return (time.time() - ga()) >= cfg.get("idle_sec", 120) if ga else True

def rebalance(block=False, aggressive=False):
    """! @brief Start a rebalance.
    @param block       wait for it to finish.
    @param aggressive  no hysteresis, idle wait or throttle (used at boot).
    """
    def work():
        run = _state["run"]
        with _state["lock"]:
            if run["active"]:
                return
            run.update(active=True, phase="planning", planned=0, done=0,
                       moved_bytes=0, errors=0, cancel=False)
        try:
            cfg = load_cfg()
            if cfg["tiers"]:
                # Relink lost objects first, even with tiering off: the objects exist.
                run["phase"] = "restore"
                adopt_ids(cfg)
                restore_orphans(cfg)
            if not cfg["enabled"] or not cfg["tiers"]:
                run["phase"] = "disabled"; return
            for t in cfg["tiers"]:
                os.makedirs(_object_root(t["path"]), exist_ok=True)
            # symlink support (Windows)
            probe = os.path.join(_object_root(cfg["tiers"][0]["path"]),
                                 ".linktest-" + uuid.uuid4().hex[:8])
            try:
                os.symlink(__file__, probe); os.remove(probe)
            except OSError as e:
                run["phase"] = f"error: symlinks unavailable ({e})"; return
            moves, _ = plan(aggressive=aggressive)
            run["planned"] = len(moves)
            run["phase"] = "moving"
            if aggressive:
                log.info(f"boot rebalance: {len(moves)} moves, unthrottled")
            for mv in moves:
                if run["cancel"]:
                    run["phase"] = "cancelled"; break
                while not aggressive and not _idle() and not run["cancel"]:
                    time.sleep(5)
                if _execute_move(mv, cfg, mbps=0 if aggressive else None):
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

def start(media_dir, db_factory, get_last_activity,
          load_stored_cfg=None, store_cfg=None,
          read_document_id=None, ensure_document_id=None):
    """! @brief Start the tiering thread.
    @param read_document_id    fn(path) -> the file's DocumentID or None.
    @param ensure_document_id  fn(abs_path, id=None) -> its DocumentID, created if missing.
    """
    _state["read_document_id"] = read_document_id
    _state["ensure_document_id"] = ensure_document_id
    _state["media_dir"] = os.path.abspath(media_dir)
    _state["db_factory"] = db_factory
    _state["get_last_activity"] = get_last_activity
    _state["load_stored_cfg"] = load_stored_cfg
    _state["store_cfg"] = store_cfg
    load_cfg()
    threading.Thread(target=_loop, daemon=True).start()

def status():
    cfg = _state["cfg"] or load_cfg()
    try:
        _, stats = plan()
    except Exception as e:
        stats = []
        log.error(f"status plan failed: {e}")
    m_files, m_bytes = _media_usage(cfg)
    media = {"path": _state.get("media_dir"), "files": m_files, "bytes": m_bytes}
    return {"config": cfg, "tiers": stats, "media": media, "run": dict(_state["run"])}