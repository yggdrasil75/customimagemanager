"""! @file
@brief Stacks module: grouping rules, raw pairing (at index and retroactively),
burst stacking from dedup groups, manual stacks, merge and split, the gallery."""
import io
import json
import time
import uuid

import numpy as np
from PIL import Image

from modules.stacks import stacks_core as sc

FOLDER = "stk_test"


# -- pure rules -------------------------------------------------------------------
def test_stem_keys_and_suffix():
    assert sc.stem_keys("a/b/IMG_1234.jxl") == ("img_1234", "img")
    assert sc.stem_keys("IMG_1234_1.jxl") == ("img_1234_1", "img_1234")
    assert sc.strip_copy_suffix("IMG_1234_7") == "IMG_1234"
    assert sc.name_key("C:\\cam\\IMG_1234.CR2") == "img_1234"
    assert sc.folder_of("x/y/z.jxl") == "x/y" and sc.folder_of("z.jxl") == ""


def _row(rel, raw=None, epoch=None):
    k, k2 = sc.stem_keys(rel)
    return {"rel_path": rel, "folder": sc.folder_of(rel), "key": k, "key2": k2,
            "raw_key": sc.name_key(raw) if raw else "", "epoch": epoch}


def test_group_raw_pairs_by_name_folder_and_time():
    rows = [_row("f/IMG_1.jxl", epoch=100), _row("f/IMG_1_1.jxl", raw="IMG_1.CR2", epoch=100),
            _row("g/IMG_1.jxl", epoch=100),                       # other folder
            _row("f/IMG_2.jxl", epoch=500), _row("f/IMG_2_1.jxl", raw="IMG_2.NEF", epoch=900),  # time guard
            _row("f/IMG_3_1.jxl", raw="IMG_3.ARW"), _row("f/IMG_3.jxl")]   # no dates: name decides
    groups = sc.group_raw(rows)
    assert (["f/IMG_1.jxl", "f/IMG_1_1.jxl"], "f/IMG_1.jxl") in groups
    assert (["f/IMG_3.jxl", "f/IMG_3_1.jxl"], "f/IMG_3.jxl") in groups
    assert not any("f/IMG_2.jxl" in m for m, _ in groups)
    assert not any("g/IMG_1.jxl" in m for m, _ in groups)


def test_group_bursts_drift_similarity_folder_album():
    info = {"a": {"folder": "f", "albums": [], "epoch": 10.0},
            "b": {"folder": "f", "albums": [], "epoch": 11.0},
            "c": {"folder": "g", "albums": ["trip"], "epoch": 12.5},
            "d": {"folder": "f", "albums": ["trip"], "epoch": 13.0},
            "e": {"folder": "f", "albums": [], "epoch": 60.0},     # drifted
            "x": {"folder": "f", "albums": [], "epoch": 11.5},     # not similar enough
            "u": {"folder": "f", "albums": [], "epoch": None}}     # undated
    dg = [{"members": ["a", "b", "c", "d", "e", "x", "u"], "scores": [1, .95, .92, .96, .97, .6, .99]}]
    out = sc.group_bursts(dg, info, max_drift=2.0, min_similarity=.9)
    # c shares the album with d, so it chains in (gap 12.5 -> 13 fits)
    assert out == [(["a", "b", "c", "d"], "a")]
    assert sc.group_bursts(dg, info, 2.0, .9, skip={"a"}) == [(["b", "c", "d"], "b")]
    assert sc.group_bursts(dg, info, 0.1, .9) == []


def test_auto_delays():
    assert sc.auto_delays([0.0, 0.25, 0.5]) == [250, 250, 250]
    assert sc.auto_delays([0.0, 0.0, 5.0]) == [100, 1000, 1000]
    assert sc.auto_delays([None, 1.0]) == [100, 100]
    assert sc.auto_delays([3.0]) == [100]


# -- app fixtures ----------------------------------------------------------------------
def _post(client, url, body=None):
    r = client.post(url, json=body if body is not None else {})
    return r.get_json()


def _stack_of(client, fn):
    j = client.get("/api/stacks/of", query_string={"filename": fn}).get_json()
    assert j["success"], j
    return j


def _set_epoch(host, fn, ep):
    host.db().execute("UPDATE files SET d_original=?, d_original_epoch=? WHERE rel_path=?",
                      (time.strftime("%Y-%m-%d", time.gmtime(ep)), ep, fn))
    host.db().commit()


def _mark_developed(host, rel, raw_name):
    """! @brief Record rel as developed from raw_name the way keep_raws does."""
    host.db().execute("INSERT OR REPLACE INTO raws(uid, path, orig_name, derived_rel, sha256, added) "
                      "VALUES(?,?,?,?,?,?)", (uuid.uuid4().hex, ".raws/x.cr2", raw_name, rel, "", time.time()))
    host.db().commit()


def _drop_raws(host, rels):
    for r in rels:
        host.db().execute("DELETE FROM raws WHERE derived_rel=?", (r,))
    host.db().commit()


def _list(client, folder):
    j = client.get("/api/list", query_string={"folder": folder}).get_json()
    assert j["success"], j
    return j["files"]


def _delete(client, *fns):
    for fn in fns:
        if fn:
            client.post("/api/delete", json={"filename": fn})


# -- raw pairing -----------------------------------------------------------------------------
def test_raw_pairs_at_upload_and_gallery_shows_one_tile(client, host, upload):
    folder = FOLDER + "_raw"
    dev = upload(name="IMG_4242.png", seed=4201, folder=folder)        # the developed raw
    try:
        _mark_developed(host, dev, "IMG_4242.CR2")
        # the camera JPEG of the same shot predicts the same stored name: it is
        # stored beside the raw (not refused as a re-upload) and paired at upload
        jpg = upload(name="IMG_4242.png", seed=4202, folder=folder)
        assert jpg != dev
        again = upload(name="IMG_4242.png", seed=4203, folder=folder, raw=True)
        assert again["duplicate"] and again["filename"] == jpg
        st = _stack_of(client, jpg)["stack"]
        assert st and st["kind"] == "raw" and st["cover"] == jpg
        assert {m["filename"] for m in st["members"]} == {jpg, dev}
        raw = [m for m in st["members"] if m["filename"] == dev][0]["raw"]
        assert raw and raw["orig_name"] == "IMG_4242.CR2"
        files = _list(client, folder)
        assert [f["filename"] for f in files] == [jpg]
        assert files[0]["stack"]["count"] == 2 and files[0]["stack"]["kind"] == "raw"
        # search token
        j = client.get("/api/list", query_string={"folder": folder, "q": "stack:raw"}).get_json()
        assert [f["filename"] for f in j["files"]] == [jpg]
        assert client.get("/api/list", query_string={"folder": folder, "q": "stack:burst"}).get_json()["files"] == []
    finally:
        _drop_raws(host, [dev])


def test_raw_pairing_retroactive_and_optout(client, host, upload):
    folder = FOLDER + "_retro"
    jpg = upload(name="DSC_0007.png", seed=4301, folder=folder)
    dev = upload(name="DSC_0007_1.png", seed=4302, folder=folder)
    other = upload(name="DSC_0008.png", seed=4303, folder=folder)
    try:
        _mark_developed(host, dev, "DSC_0007.NEF")
        assert _stack_of(client, jpg)["stack"] is None           # nothing re-indexed yet
        j = _post(client, "/api/stacks/rescan", {"raw": True, "burst": False, "wait": True})
        assert j["success"], j
        st = _stack_of(client, dev)["stack"]
        assert st and {m["filename"] for m in st["members"]} == {jpg, dev}
        assert _stack_of(client, other)["stack"] is None
        # taking a file out sticks across rescans
        j = _post(client, f"/api/stacks/{st['id']}/remove", {"filenames": [dev]})
        assert j["success"], j
        assert _stack_of(client, jpg)["stack"] is None           # one member left -> dissolved
        _post(client, "/api/stacks/rescan", {"raw": True, "burst": False, "wait": True})
        assert _stack_of(client, jpg)["stack"] is None
    finally:
        _drop_raws(host, [dev])


def test_raw_pairing_from_file_metadata_without_kept_raws(client, host, upload):
    """! @brief keep_raws off: the link is the raw name the upload writes into the file
    (XMP crs:RawFileName, which survives a JXL's sidecar)."""
    folder = FOLDER + "_xmp"
    jpg = upload(name="P1000.png", seed=4401, folder=folder)
    dev = upload(name="P1000_1.png", seed=4402, folder=folder)
    host.update_file(dev, xmp={"Xmp.crs.RawFileName": "P1000.RW2"})
    host.core.index_file(dev, force=True)
    tags = {r[0] for r in host.db().execute("SELECT tag FROM metadata_index WHERE rel_path=?", (dev,))}
    assert "crs:RawFileName" in tags
    st = _stack_of(client, jpg)["stack"]
    assert st and st["kind"] == "raw" and st["cover"] == jpg
    assert {m["filename"] for m in st["members"]} == {jpg, dev}
    assert [m["raw"] for m in st["members"] if m["filename"] == dev][0]["orig_name"] == "P1000.RW2"
    # a re-sent JPEG of that name is still the same upload
    again = upload(name="P1000.png", seed=4403, folder=folder, raw=True)
    assert again["duplicate"] and again["filename"] == jpg


def test_raw_pairing_from_exif_original_raw_file_name(client, host, upload):
    """! @brief The EXIF raw link (OriginalRawFileName) reaches a JXL's sidecar and pairs."""
    folder = FOLDER + "_exif"
    jpg = upload(name="P2000.png", seed=4411, folder=folder)
    dev = upload(name="P2000_1.png", seed=4412, folder=folder)
    res = host.update_file(dev, exif={"OriginalRawFileName": "P2000.RW2"}, history=False)
    assert res["success"] and not res["exif"]["rejected"], res
    host.core.index_file(dev, force=True)
    st = _stack_of(client, jpg)["stack"]
    assert st and st["kind"] == "raw" and {m["filename"] for m in st["members"]} == {jpg, dev}


# -- bursts ------------------------------------------------------------------------------------
def test_burst_stacks_from_dedup_groups(client, host, upload):
    folder = FOLDER + "_burst"
    fns = [upload(name=f"burst_{i}.png", seed=4500 + i, folder=folder) for i in range(5)]
    a, b, c, d, e = fns
    t0 = 1_700_000_000.0
    for fn, dt in zip(fns, (0.0, 0.4, 0.9, 100.0, 1.2)):
        _set_epoch(host, fn, t0 + dt)
    db = host.db()
    cur = db.execute("INSERT INTO dedup_groups(kind, members, scores, created) VALUES(?,?,?,?)",
                     ("similar", json.dumps([a, b, c, d, e]), json.dumps([1.0, .97, .95, .96, .55]), time.time()))
    gid = cur.lastrowid
    db.commit()
    old = (host.config.get("stacks_burst"), host.config.get("stacks_burst_drift"),
           host.config.get("stacks_burst_similarity"))
    try:
        j = _post(client, "/api/stacks/rescan", {"raw": False, "burst": True, "wait": True})
        assert not j["success"]                                   # burst stacking is off
        host.set_config("stacks_burst", True, save=False)
        host.set_config("stacks_burst_drift", 2.0, save=False)
        host.set_config("stacks_burst_similarity", 90, save=False)
        j = _post(client, "/api/stacks/rescan", {"raw": False, "burst": True, "wait": True})
        assert j["success"] and j["result"]["burst"] == 1, j
        st = _stack_of(client, a)["stack"]
        assert st["kind"] == "burst" and st["cover"] == a
        assert [m["filename"] for m in st["members"]] == [a, b, c]
        assert _stack_of(client, d)["stack"] is None and _stack_of(client, e)["stack"] is None
        assert sorted(f["filename"] for f in _list(client, folder)) == sorted([a, d, e])
        # a rescan rebuilds it the same way, a hand-picked cover keeps it
        j = _post(client, f"/api/stacks/{st['id']}/cover", {"filename": b})
        assert j["success"] and j["stack"]["cover"] == b and not j["stack"]["auto"]
        _post(client, "/api/stacks/rescan", {"raw": False, "burst": True, "wait": True})
        assert _stack_of(client, a)["stack"]["id"] == st["id"]
        st2 = _stack_of(client, a)["stack"]
        assert [f["filename"] for f in _list(client, folder) if f["filename"] in (a, b, c)] == [b]
        assert _post(client, f"/api/stacks/{st2['id']}/unstack")["success"]
    finally:
        host.set_config("stacks_burst", bool(old[0]), save=False)
        host.set_config("stacks_burst_drift", old[1] or 2.0, save=False)
        host.set_config("stacks_burst_similarity", old[2] or 90, save=False)
        db.execute("DELETE FROM dedup_groups WHERE id=?", (gid,))
        db.commit()


# -- manual stacks, merge, split --------------------------------------------------------------
def test_manual_stack_cover_remove_unstack(client, upload):
    folder = FOLDER + "_manual"
    fns = [upload(name=f"m_{i}.png", seed=4600 + i, folder=folder) for i in range(3)]
    j = _post(client, "/api/stacks/create", {"filenames": fns, "cover": fns[1]})
    assert j["success"], j
    st = j["stack"]
    assert st["kind"] == "manual" and st["cover"] == fns[1] and st["count"] == 3
    assert [f["filename"] for f in _list(client, folder)] == [fns[1]]
    assert not _post(client, "/api/stacks/create", {"filenames": [fns[0]]})["success"]
    j = _post(client, f"/api/stacks/{st['id']}/remove", {"filenames": [fns[1]]})
    assert j["success"] and j["stack"]["count"] == 2 and j["stack"]["cover"] in (fns[0], fns[2])
    assert _post(client, f"/api/stacks/{st['id']}/unstack")["success"]
    assert sorted(f["filename"] for f in _list(client, folder)) == sorted(fns)
    assert client.get(f"/api/stacks/{st['id']}").status_code == 404


def test_merge_stack_into_animation(client, host, upload):
    folder = FOLDER + "_merge"
    fns = [upload(name=f"fr_{i}.png", seed=4700 + i, folder=folder) for i in range(3)]
    sid = _post(client, "/api/stacks/create", {"filenames": fns})["stack"]["id"]
    made = []
    try:
        for fmt in ("jxl", "gif"):
            j = _post(client, f"/api/stacks/{sid}/merge", {"format": fmt, "delay_ms": 120})
            assert j["success"], j
            made.append(j["filename"])
            assert j["frames"] == 3 and j["delays_ms"] == [120, 120, 120]
            fp = host.safe_path(host.media_dir, j["filename"])
            info = host.media.jxl_anim_info(fp)
            assert info["animated"] and info["n_frames"] == 3, (fmt, j["filename"], info)
        assert not _post(client, f"/api/stacks/{sid}/merge", {"format": "bmp"})["success"]
    finally:
        _delete(client, *made)
        _post(client, f"/api/stacks/{sid}/unstack")


def _gif_bytes(n=3):
    frames = [Image.fromarray(np.random.default_rng(4800 + i).integers(0, 255, (24, 32, 3), dtype=np.uint8))
              for i in range(n)]
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    return buf.getvalue()


def test_split_animation_into_stack(client, host, upload):
    folder = FOLDER + "_split"
    r = client.post("/api/upload", data={"file": (io.BytesIO(_gif_bytes()), "anim.gif"), "mode": "sync",
                                         "folder": folder}, content_type="multipart/form-data")
    j = r.get_json()
    assert j["success"], j
    anim = j["filename"]
    made = []
    try:
        assert _stack_of(client, anim)["animated"] is True
        j = _post(client, "/api/stacks/split", {"filename": anim})
        assert j["success"], j
        made = j["files"]
        assert len(made) == 3 and j["stack"]["kind"] == "split" and j["stack"]["cover"] == made[0]
        listed = [f["filename"] for f in _list(client, folder)]
        assert made[0] in listed and made[1] not in listed and anim in listed
        still = _post(client, "/api/stacks/split", {"filename": made[0]})
        assert not still["success"]
    finally:
        _delete(client, anim, *made)


def test_deleting_a_member_shrinks_the_stack(client, upload):
    folder = FOLDER + "_del"
    fns = [upload(name=f"d_{i}.png", seed=4900 + i, folder=folder) for i in range(2)]
    sid = _post(client, "/api/stacks/create", {"filenames": fns})["stack"]["id"]
    client.post("/api/delete", json={"filename": fns[0]})
    assert client.get(f"/api/stacks/{sid}").status_code == 404
    assert _stack_of(client, fns[1])["stack"] is None


def test_status_and_assets(client):
    j = client.get("/api/stacks/status").get_json()
    assert j["success"] and set(j["counts"]) == {"raw", "burst", "manual", "split"}
    assets = client.get("/api/module_assets").get_json()["assets"]
    assert any(a["module_id"] == "stacks" and a["url"].endswith("/stacks.js") for a in assets)


def test_works_without_dedup(client, host, upload, monkeypatch):
    """! @brief Dedup is optional: with it off, raw pairing and manual stacks work, a
    burst rescan says why it can't run, and burst stacks already made are kept."""
    folder = FOLDER + "_nodedup"
    real = host.has_service
    monkeypatch.setattr(host, "has_service", lambda name: False if name == "dedup_scorers" else real(name))
    old = host.config.get("stacks_burst")
    host.set_config("stacks_burst", True, save=False)
    try:
        fns = [upload(name=f"nd_{i}.png", seed=5000 + i, folder=folder) for i in range(2)]
        sid = _post(client, "/api/stacks/create", {"filenames": fns})["stack"]["id"]
        host.db().execute("UPDATE stacks SET kind='burst', auto=1 WHERE id=?", (sid,))
        host.db().commit()
        j = _post(client, "/api/stacks/rescan", {"raw": False, "burst": True, "wait": True})
        assert not j["success"] and "Dedup" in j["error"]
        j = _post(client, "/api/stacks/rescan", {"raw": True, "burst": True, "wait": True})
        assert j["success"] and j["result"]["burst"] == 0, j
        assert _stack_of(client, fns[0])["stack"]["id"] == sid          # not dissolved
        assert client.get("/api/stacks/status").get_json()["burst"]["dedup"] is False
        jpg = upload(name="NODD_1.png", seed=5010, folder=folder)
        dev = upload(name="NODD_1_1.png", seed=5011, folder=folder)
        _mark_developed(host, dev, "NODD_1.CR2")
        host.core.index_file(dev, force=True)
        assert _stack_of(client, jpg)["stack"]["kind"] == "raw"
        _drop_raws(host, [dev])
    finally:
        host.set_config("stacks_burst", bool(old), save=False)
        _post(client, f"/api/stacks/{sid}/unstack")