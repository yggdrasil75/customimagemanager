"""! @file
@brief Integrity module: missing files (no purge), re-index on change, silent
corruption found by the deep pass, broken sidecars left untouched, accept, resolve,
and the scheduler rule."""
import hashlib
import os
import time

from cimtest import media_path

from modules.integrity import checks


def _svc(host):
    return host.get_service("integrity")


def _issue(host, fn):
    row = host.db().execute("SELECT * FROM integrity_issues WHERE rel_path=?", (fn,)).fetchone()
    return dict(row) if row else None


def _row(host, fn):
    return host.db().execute("SELECT * FROM files WHERE rel_path=?", (fn,)).fetchone()


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _flip_byte_keep_mtime(path):
    st = os.stat(path)
    with open(path, "r+b") as f:
        f.seek(st.st_size // 2)
        b = f.read(1)
        f.seek(st.st_size // 2)
        f.write(bytes([b[0] ^ 0xFF]))
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    return st


def test_missing_detected_not_purged(client, host, upload):
    fn = upload(seed=7101, name="ig_missing.png")
    os.rename(media_path(fn), media_path(fn) + ".away")
    try:
        new = {}
        assert _svc(host)["check_cheap"](fn, _row(host, fn), new, time.time(), 0) == "missing"
        assert new == {"missing": [fn]}
        i = _issue(host, fn)
        assert i["kind"] == "missing" and i["resolved"] is None
        assert _row(host, fn) is not None                     # Sync purges, not us
        j = client.get("/api/integrity/issues").get_json()
        assert j["success"] and any(x["rel_path"] == fn for x in j["issues"])
        assert j["status"]["counts"].get("missing", 0) >= 1
    finally:
        os.rename(media_path(fn) + ".away", media_path(fn))
    # back on disk: the next check resolves it
    assert _svc(host)["check_cheap"](fn, _row(host, fn), {}, time.time(), 0) != "missing"
    assert _issue(host, fn)["resolved"] is not None


def test_changed_mtime_reindexes(host, upload):
    fn = upload(seed=7102, name="ig_changed.png")
    fp = media_path(fn)
    other = upload(seed=7103, name="ig_other.png")
    with open(media_path(other), "rb") as f:
        data = f.read()
    with open(fp, "wb") as f:
        f.write(data)
    st = os.stat(fp)
    os.utime(fp, (st.st_atime, st.st_mtime + 10))
    assert _svc(host)["check_cheap"](fn, _row(host, fn), {}, time.time(), 0) == "reindexed"
    r = _row(host, fn)
    assert r["sha256"] == _sha(fp) and abs(r["mtime"] - os.path.getmtime(fp)) < 0.01


def test_silent_corruption_found_by_deep_pass_then_accept(client, host, upload):
    fn = upload(seed=7104, name="ig_rot.png")
    fp = media_path(fn)
    before = _row(host, fn)["sha256"]
    _flip_byte_keep_mtime(fp)
    # the cheap pass sees nothing (same size, same mtime) and does not re-index
    assert _svc(host)["check_cheap"](fn, _row(host, fn), {}, time.time(), 0) == "ok"
    assert _row(host, fn)["sha256"] == before
    new = {}
    assert _svc(host)["check_deep"](fn, new, time.time(), mb_per_s=0) == "corrupt"
    assert new["corrupt"] == [fn]
    assert _issue(host, fn)["kind"] == "corrupt"
    # accept: the admin says the change was intentional
    j = client.post("/api/integrity/accept", json={"rel_path": fn}).get_json()
    assert j["success"] and j["sha256"] == _sha(fp)
    assert _row(host, fn)["sha256"] == _sha(fp)
    assert _issue(host, fn)["resolved"] is not None
    assert _svc(host)["check_deep"](fn, {}, time.time(), mb_per_s=0, decode=False) == "ok"


def test_resolved_when_clean_again(host, upload):
    fn = upload(seed=7105, name="ig_heal.png")
    fp = media_path(fn)
    with open(fp, "rb") as f:
        good = f.read()
    st = _flip_byte_keep_mtime(fp)
    assert _svc(host)["check_deep"](fn, {}, time.time(), mb_per_s=0, decode=False) == "corrupt"
    with open(fp, "wb") as f:                                  # restored from a backup copy
        f.write(good)
    os.utime(fp, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert _svc(host)["check_deep"](fn, {}, time.time(), mb_per_s=0, decode=False) == "ok"
    i = _issue(host, fn)
    assert i["kind"] == "corrupt" and i["resolved"] is not None
    assert _open_issue_gone(host, fn)


def _open_issue_gone(host, fn):
    """! @brief A resolved issue is not in the open list."""
    return host.db().execute("SELECT 1 FROM integrity_issues WHERE rel_path=? AND resolved IS NULL",
                             (fn,)).fetchone() is None


def test_broken_sidecar_reported_and_untouched(client, host, upload):
    fn = upload(seed=7106, name="ig_side.png")
    side = os.path.splitext(media_path(fn))[0] + ".xmp"
    junk = "<x:xmpmeta><rdf:RDF>not closed"
    with open(side, "w") as f:
        f.write(junk)
    st = os.stat(side)
    os.utime(side, (st.st_atime, st.st_mtime + 20))
    tags_before = _row(host, fn)["tags"]
    assert _svc(host)["check_cheap"](fn, _row(host, fn), {}, time.time(), 0) == "sidecar"
    assert _issue(host, fn)["kind"] == "sidecar"
    j = client.post("/api/integrity/recheck", json={"rel_path": fn}).get_json()
    assert j["success"] and j["issue"]["kind"] == "sidecar" and not j["issue"]["resolved"]
    with open(side) as f:
        assert f.read() == junk                               # never rewritten
    assert _row(host, fn)["tags"] == tags_before              # nor indexed from


def test_decode_failure_reported(client, host, upload):
    fn = upload(seed=7107, name="ig_decode.png")
    fp = media_path(fn)
    st = os.stat(fp)
    with open(fp, "r+b") as f:                                 # keep the size, wreck the content
        f.write(b"\0" * min(st.st_size, 4096))
    os.utime(fp, ns=(st.st_atime_ns, st.st_mtime_ns))
    svc = _svc(host)
    svc["check_deep"](fn, {}, time.time(), mb_per_s=0, decode=True)
    assert _issue(host, fn)["kind"] == "corrupt"          # outranks decode
    # once the new content is accepted, the decode failure is what remains
    assert client.post("/api/integrity/accept", json={"rel_path": fn}).get_json()["success"]
    new = {}
    assert svc["check_deep"](fn, new, time.time(), mb_per_s=0, decode=True) == "ok"
    i = _issue(host, fn)
    assert new == {"decode": [fn]} and i["kind"] == "decode" and i["resolved"] is None


def test_plan_tick_interval_and_idle():
    cfg = {"enabled": True, "cheap_minutes": 60, "deep_enabled": True, "deep_days": 30}
    now = 1_000_000.0
    idle_state = {"cheap_cursor": None, "cheap_started": now - 30, "cheap_tick": 0,
                  "deep_cursor": None, "deep_started": now - 3600}
    # a cheap cycle finished recently, the deep one too: nothing to do
    assert checks.plan_tick(now, idle_state, cfg, idle=True) == (None, False)
    # the interval passed: a new cheap cycle
    st = dict(idle_state, cheap_started=now - 3601)
    assert checks.plan_tick(now, st, cfg, idle=False) == ("cheap", True)
    # mid-cycle: the next batch, but not faster than the tick gap
    st = dict(idle_state, cheap_cursor="a.png", cheap_tick=now - 10)
    assert checks.plan_tick(now, st, cfg, idle=False) == ("cheap", False)
    st = dict(st, cheap_tick=now - 0.5)
    assert checks.plan_tick(now, st, cfg, idle=False) == (None, False)
    # the deep pass needs idle, no tier move, and its interval
    deep_due = dict(idle_state, deep_started=now - 31 * 86400)
    assert checks.plan_tick(now, deep_due, cfg, idle=False) == (None, False)
    assert checks.plan_tick(now, deep_due, cfg, idle=True, tier_busy=True) == (None, False)
    assert checks.plan_tick(now, deep_due, cfg, idle=True) == ("deep", True)
    mid = dict(idle_state, deep_cursor="")
    assert checks.plan_tick(now, mid, cfg, idle=True) == ("deep", False)
    assert checks.plan_tick(now, mid, dict(cfg, deep_enabled=False), idle=True) == (None, False)
    # one pass at a time; disabled = nothing
    assert checks.plan_tick(now, deep_due, cfg, idle=True, running=True) == (None, False)
    assert checks.plan_tick(now, deep_due, dict(cfg, enabled=False), idle=True) == (None, False)


def test_hash_throttle_and_abort(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(os.urandom(3 * checks.CHUNK))
    slept = []
    assert checks.hash_file(str(p), 1, sleep=slept.append) == _sha(str(p))
    assert sum(slept) > 2.0                                    # 3 MB at 1 MB/s
    assert checks.hash_file(str(p), 0, abort=lambda: True) is None


def test_sidecar_rule_and_severity():
    assert checks.sidecar_needs_index(100.0, 50.0, None, 0) is False       # first sight: baseline
    assert checks.sidecar_needs_index(120.0, 50.0, 100.0, 200.0) is False  # synced since
    assert checks.sidecar_needs_index(100.0, 50.0, 100.0, 0) is False      # in-app write seen
    assert checks.sidecar_needs_index(120.0, 50.0, 100.0, 0) is True
    assert checks.sidecar_needs_index(0.0, 50.0, None, 0) is False
    assert checks.severity("missing") < checks.severity("corrupt") < checks.severity("decode")


def test_worker_claim_waits_after_boot(host):
    # the first pass waits FIRST_DELAY after startup, so tests and boot are left alone
    assert _svc(host)["claim"]() is None


def test_outside_sidecar_edit_reindexes(host, upload):
    fn = upload(seed=7108, name="ig_sideedit.png")
    side = os.path.splitext(media_path(fn))[0] + ".xmp"
    svc = _svc(host)
    assert svc["check_cheap"](fn, _row(host, fn), {}, time.time(), 0) == "ok"      # baseline
    assert svc["check_cheap"](fn, _row(host, fn), {}, time.time(), 0) == "ok"
    st = os.stat(side)
    os.utime(side, (st.st_atime, st.st_mtime + 30))                               # edited elsewhere
    assert svc["check_cheap"](fn, _row(host, fn), {}, time.time(), 0) == "reindexed"
    # an in-app write is not mistaken for an outside edit
    assert host.update_file(fn, add={"tags": ["ig_tag"]})["success"]
    assert svc["check_cheap"](fn, _row(host, fn), {}, time.time(), 0) == "ok"


def test_cheap_pass_cursor_resumes(host, upload):
    for i in range(3):
        upload(seed=7110 + i, name="ig_batch%d.png" % i)
    old = host.config.get("integrity_cheap_batch")
    host.set_config("integrity_cheap_batch", 10, save=False)
    try:
        total = host.db().execute("SELECT COUNT(*) FROM files").fetchone()[0]
        svc = _svc(host)
        svc["run_cheap"](True)
        st = svc["status"]()
        if total > 10:
            assert st["cheap"]["in_cycle"] and st["cheap"]["done"] == 10
        for _ in range(total // 10 + 1):
            if not svc["status"]()["cheap"]["in_cycle"]:
                break
            svc["run_cheap"](False)
        st = svc["status"]()
        assert not st["cheap"]["in_cycle"] and st["cheap"]["finished"]
    finally:
        host.set_config("integrity_cheap_batch", old or 200, save=False)
