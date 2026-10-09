"""! @file
@brief Integrity module, the media walk: files put in the media folder outside the
app are adopted (indexed in place, never converted); zero-byte, partial, mislabeled,
undecodable, unsupported files, orphan sidecars and raws / HEIF without a decoder
are reported with their reason; manual purge moves them to the trash bin or the
quarantine folder; auto purge only takes old leftovers; invalid rows."""
import io
import os
import shutil
import time

import cv2
import numpy as np
import pytest
from PIL import Image

from cimtest import media_path

from modules.integrity import checks

WEEK = 8 * 86400


def _svc(host):
    return host.get_service("integrity")


def _issue(host, rel):
    row = host.db().execute("SELECT * FROM integrity_issues WHERE rel_path=?", (rel,)).fetchone()
    return dict(row) if row else None


def _row(host, rel):
    return host.db().execute("SELECT * FROM files WHERE rel_path=?", (rel,)).fetchone()


def _img(seed, w=48, h=32):
    rng = np.random.default_rng(seed)
    return rng.integers(0, 255, (h, w, 3), dtype=np.uint8)


def _jpeg(seed):
    return cv2.imencode(".jpg", _img(seed))[1].tobytes()


def _png(seed):
    return cv2.imencode(".png", _img(seed))[1].tobytes()


def _avif(seed):
    buf = io.BytesIO()
    Image.fromarray(_img(seed)).save(buf, format="AVIF")
    return buf.getvalue()


def _put(folder, name, data, age=0.0):
    """! @brief Write a file into the media folder as an outside tool would; `age` backdates it."""
    rel = folder + "/" + name
    fp = media_path(rel)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "wb") as f:
        f.write(data)
    if age:
        t = time.time() - age
        os.utime(fp, (t, t))
    return rel


@pytest.fixture
def stray_dir(host):
    """! @brief A fresh folder for strays; rows, issues and files are cleaned after."""
    folder = "ig_strays_%d" % int(time.time() * 1000)
    yield folder
    for (rel,) in host.db().execute("SELECT rel_path FROM files WHERE rel_path LIKE ?",
                                    (folder + "/%",)).fetchall():
        host.core.purge_file_everywhere(rel)
    host.update_file(table="integrity_issues", where=("rel_path LIKE ?", (folder + "/%",)),
                     remove=True, dont_write=True)
    shutil.rmtree(media_path(folder), ignore_errors=True)


@pytest.fixture
def no_trash(host):
    """! @brief The trash module's file.trash handler out of the way (quarantine path)."""
    saved = list(host.event_hooks.get("file.trash", []))
    host.event_hooks["file.trash"] = []
    yield
    host.event_hooks["file.trash"] = saved


def test_jpeg_and_avif_adopted_in_place(host, client, stray_dir):
    jpg = _put(stray_dir, "camera.jpg", _jpeg(1))
    avif = _put(stray_dir, "phone.avif", _avif(2))
    got = _svc(host)["scan"](stray_dir)
    assert got["adopted"] == 2 and sorted(got["recent"]) == sorted([jpg, avif])
    for rel, (w, h) in ((jpg, (48, 32)), (avif, (48, 32))):
        r = _row(host, rel)
        assert r is not None and (r["width"], r["height"]) == (w, h) and r["media_kind"] == "image"
        assert os.path.exists(media_path(rel))                       # never converted
        assert not os.path.exists(os.path.splitext(media_path(rel))[0] + ".jxl")
        assert _issue(host, rel) is None
        assert client.get("/api/thumb/" + rel).status_code == 200
        assert client.get("/api/file/" + rel).status_code == 200
    st = client.get("/api/integrity/issues").get_json()["status"]["walk"]
    assert st["adopted_total"] >= 2 and jpg in st["recent_adopted"]
    # a second walk finds them indexed: nothing adopted twice
    assert _svc(host)["scan"](stray_dir).get("adopted", 0) == 0


def test_invalid_files_reported_with_reasons(host, client, stray_dir):
    rels = {
        "zero_byte": _put(stray_dir, "empty.jpg", b""),
        "temp": _put(stray_dir, "movie.mp4.part", b"partial bytes"),
        "mislabeled": _put(stray_dir, "lie.jpg", _png(3)),
        "orphan_sidecar": _put(stray_dir, "gone.xmp", b"<x:xmpmeta xmlns:x='adobe:ns:meta/'/>"),
        "undecodable": _put(stray_dir, "broken.png", b"\x89PNG\r\n\x1a\n" + b"\0" * 64),
        "unsupported": _put(stray_dir, "notes.psd", b"8BPS" + b"\0" * 32),
    }
    kept = _put(stray_dir, "kept.jpg", _jpeg(4))
    _put(stray_dir, "kept.xmp", b"<x:xmpmeta xmlns:x='adobe:ns:meta/'/>")  # has its owner
    _put(stray_dir, "info.json", b"{}")                                     # benign
    got = _svc(host)["scan"](stray_dir)
    assert got["adopted"] == 1 and got["invalid"] == len(rels)
    for code, rel in rels.items():
        i = _issue(host, rel)
        assert i is not None and i["kind"] == "invalid" and i["resolved"] is None, (code, i)
        assert checks.reason_code(i["detail"]) == code, (code, i["detail"])
        assert _row(host, rel) is None                                # never indexed
    assert _issue(host, kept) is None and _issue(host, stray_dir + "/kept.xmp") is None
    assert _issue(host, stray_dir + "/info.json") is None
    j = client.get("/api/integrity/issues").get_json()
    assert j["status"]["counts"].get("invalid", 0) >= len(rels)
    # a reported file that disappears is resolved by the next walk
    os.remove(media_path(rels["unsupported"]))
    _svc(host)["scan"](stray_dir)
    assert _issue(host, rels["unsupported"])["resolved"] is not None


def test_heic_without_decoder_skipped(host, stray_dir):
    if host.media._HAVE_PILLOW_HEIF:
        pytest.skip("pillow-heif installed: HEIC is a library kind here")
    heic = _put(stray_dir, "iphone.heic", b"\0\0\0\x18ftypheic\0\0\0\0mif1heic" + b"\0" * 64)
    assert not host.media.is_library_file(heic)
    got = _svc(host)["scan"](stray_dir)
    assert got.get("adopted", 0) == 0 and _row(host, heic) is None
    i = _issue(host, heic)
    assert checks.reason_code(i["detail"]) == "no_decoder" and "pillow-heif" in i["detail"]


def test_raw_without_decoder_reported(host, stray_dir):
    if host.media._HAVE_RAWPY:
        pytest.skip("rawpy installed: raws are indexed here")
    raw = _put(stray_dir, "DSC_0001.NEF", b"II*\0" + b"\0" * 64)
    _svc(host)["scan"](stray_dir)
    assert _row(host, raw) is None
    assert checks.reason_code(_issue(host, raw)["detail"]) == "no_decoder"


def test_manual_purge_to_trash(host, client, stray_dir):
    if not host.has_service("trash"):
        pytest.skip("trash module off")
    part = _put(stray_dir, "dl.jpg.crdownload", b"xx")
    side = _put(stray_dir, "lost.xmp", b"<x/>")
    _svc(host)["scan"](stray_dir)
    j = client.post("/api/integrity/purge", json={"rel_paths": [part, side]}).get_json()
    assert j["success"] and sorted(p["rel_path"] for p in j["purged"]) == sorted([part, side])
    assert all(p["to"] == "trash" for p in j["purged"])
    assert not os.path.exists(media_path(part)) and not os.path.exists(media_path(side))
    rows = host.db().execute("SELECT rel_path FROM trash_items WHERE rel_path IN (?, ?)",
                             (part, side)).fetchall()
    assert len(rows) == 2
    assert _issue(host, part)["resolved"] is not None
    # only invalid issues are purgeable
    assert client.post("/api/integrity/purge", json={"rel_paths": [part]}).get_json()["skipped"] == [part]


def test_manual_purge_to_quarantine(host, client, stray_dir, no_trash):
    bad = _put(stray_dir, "x.jpg", b"")
    sib = _put(stray_dir, "x.jxl", b"\xff\x0a")      # a same-stem sibling must stay put
    _svc(host)["scan"](stray_dir)
    assert checks.reason_code(_issue(host, bad)["detail"]) == "zero_byte"
    j = client.post("/api/integrity/purge", json={"rel_paths": [bad]}).get_json()
    assert j["success"] and j["purged"][0]["to"].startswith(".cim/quarantine/")
    q = os.path.join(host.media_dir, j["purged"][0]["to"], "x.jpg")
    assert os.path.exists(q) and not os.path.exists(media_path(bad))
    assert os.path.exists(media_path(sib))
    os.remove(q)


def test_auto_purge_only_eligible_leftovers(host, stray_dir, no_trash):
    old_tmp = _put(stray_dir, "old.tmp", b"x", age=WEEK)
    new_tmp = _put(stray_dir, "new.tmp", b"x", age=3600)
    old_empty = _put(stray_dir, "old_empty.png", b"", age=WEEK)
    old_side = _put(stray_dir, "old_orphan.xmp", b"<x/>", age=WEEK)
    old_lie = _put(stray_dir, "old_lie.jpg", _png(5), age=WEEK)       # mislabeled: report only
    old_psd = _put(stray_dir, "old.psd", b"8BPS", age=WEEK)          # unsupported: report only
    host.set_config("integrity_auto_purge", True, save=False)
    try:
        got = _svc(host)["scan"](stray_dir)
    finally:
        host.set_config("integrity_auto_purge", False, save=False)
    assert got["auto_purged"] == 3
    for rel in (old_tmp, old_empty, old_side):
        assert not os.path.exists(media_path(rel))
    for rel in (new_tmp, old_lie, old_psd):
        assert os.path.exists(media_path(rel))
        assert _issue(host, rel)["resolved"] is None
    shutil.rmtree(os.path.join(host.media_dir, ".cim", "quarantine", time.strftime("%Y-%m-%d"), stray_dir),
                  ignore_errors=True)


def test_invalid_rows(host, client, stray_dir):
    rel = _put(stray_dir, "flat.png", _png(6))
    assert host.core.index_file(rel, force=True)
    host.update_file(rel, db={"width": 0, "height": 0}, dont_write=True)
    svc = _svc(host)
    new = {}
    svc["check_cheap"](rel, _row(host, rel), new, time.time(), 0)
    i = _issue(host, rel)
    assert i["kind"] == "invalid_row" and checks.reason_code(i["detail"]) == "bad_dims"
    # purge drops the row only; the walk indexes the file again with real dimensions
    j = client.post("/api/integrity/purge", json={"kind": "invalid_row"}).get_json()
    assert any(p["rel_path"] == rel and p["to"] == "rows" for p in j["purged"])
    assert _row(host, rel) is None and os.path.exists(media_path(rel))
    svc["scan"](stray_dir)
    assert (_row(host, rel)["width"], _row(host, rel)["height"]) == (48, 32)
    # a row whose file is no library kind any more
    odd = _put(stray_dir, "odd.psd", b"8BPS")
    db = host.db()                                    # a row left from an older install
    db.execute("INSERT INTO files (rel_path, mtime, width, height) VALUES (?, ?, 1, 1)",
               (odd, os.path.getmtime(media_path(odd))))
    db.commit()
    svc["check_cheap"](odd, _row(host, odd), {}, time.time(), 0)
    i = _issue(host, odd)
    assert i["kind"] == "invalid_row" and checks.reason_code(i["detail"]) == "not_library"


def test_walk_runs_in_the_cheap_cycle(host, stray_dir):
    rel = _put(stray_dir, "cycle.jpg", _jpeg(7), age=3600)
    svc = _svc(host)
    old = host.config.get("integrity_cheap_batch")
    host.set_config("integrity_cheap_batch", 10000, save=False)
    try:
        svc["run_cheap"](True)
        for _ in range(500):
            if not svc["status"]()["cheap"]["in_cycle"]:
                break
            svc["run_cheap"](False)
    finally:
        host.set_config("integrity_cheap_batch", old or 200, save=False)
    assert not svc["status"]()["cheap"]["in_cycle"]
    assert _row(host, rel) is not None
    assert svc["status"]()["walk"]["last"]["finished"]


def test_rules():
    assert checks.temp_reason("a.JPG.part") and checks.temp_reason("~$doc.docx")
    assert checks.temp_reason("x.!qB") and not checks.temp_reason("a.jpg")
    assert checks.is_orphan_sidecar("a.xmp", {"a.xmp", "b.jpg"})
    assert not checks.is_orphan_sidecar("a.xmp", {"a.xmp", "a.jxl"})
    assert not checks.is_orphan_sidecar("a.jpg.xmp", {"a.jpg.xmp", "a.jpg"})
    assert checks.is_orphan_sidecar("a.xmp", {"a.xmp", "a.txt"})
    now = 10 * 86400.0
    assert checks.auto_purge_ok("temp", 0, now) and not checks.auto_purge_ok("temp", now - 60, now)
    assert not checks.auto_purge_ok("mislabeled", 0, now)
    assert checks.reason_code(checks.detail("zero_byte", "x")) == "zero_byte"
    assert checks.reason_code("free text") == ""
