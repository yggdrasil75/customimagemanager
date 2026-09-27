"""Google Takeout import through the fetch module: sidecars across split zips,
truncated/renamed sidecars, album copies imported once, edited copies, live
photos, trash, dates/GPS where the file has none; a watched folder imports a
new Takeout by itself and re-runs import nothing twice."""
import json, os, shutil, zipfile
import pytest
from cimtest import png_bytes, read_meta
from modules.fetch.tests.importtest import imports, ledger, run_source  # noqa: F401  (fixture)

G = "Takeout/Google Photos/"
LONG = "a_really_long_holiday_photo_name_from_camera_2019"      # Takeout truncates long names


def _side(title, ts, **kw):
    d = {"title": title, "photoTakenTime": {"timestamp": str(ts)}, "creationTime": {"timestamp": "1"},
         "geoData": {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0}}
    d.update(kw)
    return json.dumps(d).encode()


def write_takeout(root, stamp="20260101T000000Z", seed0=500):
    """Two zips; photos and their sidecars deliberately split across them."""
    b = lambda i: png_bytes(seed=seed0 + i)
    y = G + "Photos from 2019/"
    z1 = {y + "beach.png": b(1), y + "family.png": b(2), y + "cake.png": b(4), y + "cake-edited.png": b(3),
          y + "IMG.png": b(5), y + "IMG(1).png": b(6), y + "gone.png": b(7), y + LONG[:43] + ".png": b(8),
          y + "live.png": b(9), y + "live.mp4": b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64,
          G + "Beach trip/beach.png": b(1), G + "Beach trip/family.png": b(2)}
    z2 = {y + "beach.png.supplemental-metadata.json": _side("beach.png", 1563620625,
              geoData={"latitude": 37.7712, "longitude": -122.4567, "altitude": 12.0},
              description="first day", favorited=True, people=[{"name": "Mom"}]),
          y + "family.png.json": _side("family.png", 1563707025, archived=True),
          y + "cake.png.supplemental-met.json": _side("cake.png", 1563793425),
          y + "IMG.png.supplemental-metadata.json": _side("IMG.png", 1563800000),
          y + "IMG.png.supplemental-metadata(1).json": _side("IMG.png", 1563900000),
          y + "gone.png.json": _side("gone.png", 1563900001, trashed=True),
          y + LONG[:43] + ".png.s.json": _side(LONG + ".png", 1564000000),
          y + "live.png.json": _side("live.png", 1564100000),
          G + "Beach trip/metadata.json": json.dumps({"title": "Beach trip", "date": {"timestamp": "1"}}).encode(),
          G + "Beach trip/beach.png.json": _side("beach.png", 1563620625),
          G + "print-subscriptions.json": b"{}"}
    for i, files in ((1, z1), (2, z2)):
        with zipfile.ZipFile(os.path.join(root, f"takeout-{stamp}-00{i}.zip"), "w") as z:
            for n, data in files.items():
                z.writestr(n, data)


def _save(client, **config):
    r = client.post("/api/import/google_photos/save", json={"config": {"settle_min": 0, **config}})
    assert r.status_code == 200, r.get_json()
    return r.get_json()["id"]


def _file(app, name):
    return ledger(app, "google_photos", name)


def test_browse_and_path_confinement(client, imports):
    write_takeout(str(imports))
    names = {e["name"] for e in client.get("/api/import/google_photos/browse").get_json()["entries"]}
    assert names == {"takeout-20260101T000000Z-001.zip", "takeout-20260101T000000Z-002.zip"}
    r = client.post("/api/import/google_photos/save", json={"config": {"path": "../../etc"}})
    assert r.status_code == 400 and "outside" in r.get_json()["error"]


def test_takeout_import(client, app, imports):
    sub = imports / "takeouts"; sub.mkdir()
    write_takeout(str(sub))
    sid = _save(client, path="takeouts", folder="google-photos/{year}")
    row = run_source(client, app, "google_photos", sid)
    assert row["status"] == "done", row
    st = {r["status"]: r["n"] for r in app._db().execute(
        "SELECT status, COUNT(*) n FROM fetch_items WHERE fetcher='google_photos' AND item_key NOT LIKE 'set:%' "
        "GROUP BY status")}
    assert st.get("done") == 9 and st.get("skipped") == 1 and not st.get("failed"), (st, row)

    beach = _file(app, "beach.png")
    assert beach["d_original"] == "2019-07-20" and beach["rel_path"].startswith("google-photos/2019/")
    m = read_meta(client, beach["rel_path"])
    assert "favorite" in m["tags"] and "people:Mom" in m["tags"] and m["description"] == "first day"
    assert "Beach trip" in app._file_albums(beach["rel_path"])
    xmp = open(os.path.join(app.MEDIA_DIR, os.path.splitext(beach["rel_path"])[0] + ".xmp")).read()
    assert "37,46.272" in xmp and "122,27.402" in xmp and "W" in xmp

    fam = _file(app, "family.png")
    assert "archived" in read_meta(client, fam["rel_path"])["tags"]
    assert "Beach trip" in app._file_albums(fam["rel_path"])           # album copy: one file, album added
    assert app._db().execute("SELECT COUNT(*) FROM files WHERE rel_path LIKE 'google-photos/%family%'").fetchone()[0] == 1
    assert "edited" in read_meta(client, _file(app, "cake-edited.png")["rel_path"])["tags"]
    assert _file(app, "cake-edited.png")["d_original"] == "2019-07-22"  # inherits the original's sidecar
    a, b = _file(app, "IMG.png"), _file(app, "IMG(1).png")
    assert a["rel_path"] != b["rel_path"] and {a["d_original"], b["d_original"]} == {"2019-07-22", "2019-07-23"}
    assert _file(app, LONG + ".png")["status"] == "done"                # truncated name restored from the sidecar
    assert _file(app, "live.mp4")["status"] == "done"                   # live-photo video as companion
    assert _file(app, "gone.png")["status"] == "skipped"


def test_watched_folder_only_new(client, app, imports):
    sub = imports / "takeouts"; sub.mkdir()
    write_takeout(str(sub))
    sid = _save(client, path="takeouts", every_h=6)
    w = app._db().execute("SELECT every_h, enabled FROM fetch_watch WHERE target=?", (f"google_photos:{sid}",)).fetchone()
    assert w["every_h"] == 6 and w["enabled"] == 1
    run_source(client, app, "google_photos", sid)
    n1 = app._db().execute("SELECT COUNT(*) FROM upload_queue").fetchone()[0]
    row = run_source(client, app, "google_photos", sid)                # nothing changed: set skipped
    assert app._db().execute("SELECT COUNT(*) FROM upload_queue").fetchone()[0] == n1 and row["downloaded"] == 0
    # a newer Takeout lands: overlapping photos are known by content, only new ones go in
    write_takeout(str(sub), stamp="20260301T000000Z", seed0=500)
    with zipfile.ZipFile(str(sub / "takeout-20260301T000000Z-001.zip"), "a") as z:
        z.writestr(G + "Photos from 2026/new.png", png_bytes(seed=599))
    row = run_source(client, app, "google_photos", sid)
    assert row["downloaded"] == 1, row
    assert _file(app, "new.png")["status"] == "done"


def test_edited_mode_original_then_both(client, app, imports):
    write_takeout(str(imports))
    sid = _save(client, path="takeout-20260101T000000Z-001.zip", edited="original")
    # a single zip of a split Takeout: sidecars are in part 2, so point at the folder instead
    client.post("/api/import/google_photos/save", json={"id": sid, "config": {"path": "", "settle_min": 0}})
    sid = _save(client, path=".", edited="original")
    run_source(client, app, "google_photos", sid)
    assert _file(app, "cake-edited.png")["status"] == "skipped"
    client.post("/api/import/google_photos/save", json={"id": sid, "config": {"path": ".", "edited": "both", "settle_min": 0}})
    app._db().execute("DELETE FROM fetch_items WHERE item_key LIKE 'set:%'"); app._db().commit()
    run_source(client, app, "google_photos", sid)
    assert _file(app, "cake-edited.png")["status"] == "done"


def test_later_edits_keep_imported_date_and_gps(client, app, imports):
    """Regression: editing tags or albums rewrote the sidecar from scratch and
    erased the imported capture date and GPS."""
    from cimtest import write_meta
    write_takeout(str(imports))
    sid = _save(client, path=".")
    run_source(client, app, "google_photos", sid)
    rel = _file(app, "beach.png")["rel_path"]
    write_meta(client, rel, tags=["edited-later"], desc="new caption")
    client.post("/api/albums/add", json={"album": "Later", "files": [rel]})
    app._index_file(rel, force=True)
    row = app._db().execute("SELECT d_original FROM files WHERE rel_path=?", (rel,)).fetchone()
    assert row["d_original"] == "2019-07-20"
    xmp = open(os.path.join(app.MEDIA_DIR, os.path.splitext(rel)[0] + ".xmp")).read()
    assert "37,46.272" in xmp and "edited-later" in xmp and "Later" in xmp
