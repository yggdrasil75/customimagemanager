"""! @file
@brief upload.py --takeout: a Google Takeout read on the uploading machine (zips,
an extracted folder or a mix) goes up through the normal upload with the same
results as the server-side importer: dates, GPS, descriptions, people,
favourite / archived flags, albums, album copies sent once, edited copies, live
photos, trash left out; a date the photo's own EXIF has is kept."""
import json
import os
import sys
import threading
import zipfile

from werkzeug.serving import make_server

from cimtest import png_bytes, read_meta
from modules.google_photos_import.tests.test_google_photos_import import G, LONG, _side, write_takeout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import upload as upcli  # noqa: E402


def _clear(app, client, folder):
    """! @brief Remove what a test uploaded, so later tests see an empty folder."""
    import shutil
    for r in app._db().execute("SELECT rel_path FROM files WHERE rel_path LIKE ?", (folder + "/%",)).fetchall():
        client.post("/api/delete", json={"filename": r["rel_path"], "permanent": True})
    shutil.rmtree(os.path.join(app.MEDIA_DIR, folder), ignore_errors=True)


def _plan(src, mode="both"):
    plan, left = upcli.takeout_plan(upcli.takeout_entries(str(src)), "google-photos", mode)
    return dict((p["name"], p) for _e, p in plan), dict(left)


def _unzip_second(src):
    """! @brief The sidecar zip extracted beside the photo zip, as half-unpacked downloads are."""
    z2 = next(p for p in sorted(os.listdir(src)) if p.endswith("-002.zip"))
    with zipfile.ZipFile(os.path.join(src, z2)) as z:
        z.extractall(os.path.join(src, "unpacked"))
    os.remove(os.path.join(src, z2))


def test_plan_reads_every_field(tmp_path):
    write_takeout(str(tmp_path))
    _unzip_second(str(tmp_path))
    by_name, left = _plan(tmp_path)
    beach = by_name["beach.png"]
    assert beach["folder"] == "google-photos/2019"
    m = beach["metadata"]
    assert m["description"] == "first day" and "favorite" in m["tags"] and "people:Mom" in m["tags"]
    assert m["albums"] == ["Beach trip"]
    assert m["xmp_fallback"]["exif:DateTimeOriginal"] == "2019-07-20T11:03:45Z"
    assert m["xmp_fallback"]["exif:GPSLatitude"] == "37,46.272000N"
    assert m["xmp_fallback"]["exif:GPSLongitude"] == "122,27.402000W"
    assert "archived" in by_name["family.png"]["metadata"]["tags"]
    assert by_name["family.png"]["metadata"]["albums"] == ["Beach trip"]
    assert sum(1 for n in by_name if n == "family.png") == 1          # the album copy is the same photo
    assert "edited" in by_name["cake-edited.png"]["metadata"]["tags"]
    assert by_name["cake-edited.png"]["metadata"]["xmp_fallback"]["exif:DateTimeOriginal"].startswith("2019-07-22")
    assert {by_name["IMG.png"]["metadata"]["xmp_fallback"]["exif:DateTimeOriginal"][:10],
            by_name["IMG(1).png"]["metadata"]["xmp_fallback"]["exif:DateTimeOriginal"][:10]} == \
        {"2019-07-22", "2019-07-23"}
    assert LONG + ".png" in by_name                                     # truncated name restored
    assert "live-photo" in by_name["live.mp4"]["metadata"]["tags"]
    assert by_name["live.mp4"]["metadata"]["xmp_fallback"] == by_name["live.png"]["metadata"]["xmp_fallback"]
    assert "gone.png" not in by_name and any("trash" in why for why in left.values())
    assert "geoData" not in json.dumps(by_name["cake.png"]["metadata"])  # (0, 0) is "no position"
    assert "exif:GPSLatitude" not in by_name["cake.png"]["metadata"]["xmp_fallback"]


import pytest


@pytest.mark.parametrize("layout", ["one_folder", "folder_per_zip", "folder_per_zip_no_takeout_dir"])
def test_every_extraction_layout_reads_the_same(tmp_path, layout):
    """! @brief Zips unpacked into one folder, each into its own takeout-...-00N/ folder,
    or with the inner Takeout/ folder dropped: sidecars from another part still match."""
    zips = tmp_path / "zips"
    zips.mkdir()
    write_takeout(str(zips))
    src = tmp_path / "src"
    for zp in sorted(zips.iterdir()):
        with zipfile.ZipFile(str(zp)) as z:
            for info in z.infolist():
                name = info.filename
                if layout == "folder_per_zip_no_takeout_dir":
                    name = name.split("/", 1)[1]
                base = src if layout == "one_folder" else src / zp.stem
                out = base / name
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(z.read(info))
    by_name, left = _plan(src)
    assert by_name["beach.png"]["metadata"]["description"] == "first day"
    assert by_name["beach.png"]["metadata"]["albums"] == ["Beach trip"]
    assert by_name["family.png"]["metadata"]["albums"] == ["Beach trip"]
    assert LONG + ".png" in by_name and "gone.png" not in by_name
    assert len(by_name) == 9


def test_plan_edited_modes(tmp_path):
    write_takeout(str(tmp_path))
    orig, left = _plan(tmp_path, "original")
    assert "cake-edited.png" not in orig and "cake.png" in orig
    edited, left = _plan(tmp_path, "edited")
    assert "cake-edited.png" in edited and "cake.png" not in edited


def test_json_target_names():
    assert upcli.takeout_json_target("IMG.jpg.supplemental-metadata(1).json") == ("IMG.jpg", 1)
    assert upcli.takeout_json_target("IMG.jpg(1).json") == ("IMG.jpg", 1)
    assert upcli.takeout_json_target("IMG.jpg.sup.json") == ("IMG.jpg", None)


def test_takeout_hint_without_the_flag(tmp_path):
    write_takeout(str(tmp_path))
    assert upcli.looks_like_takeout(str(tmp_path))
    (tmp_path / "plain").mkdir()
    (tmp_path / "plain" / "a.png").write_bytes(b"x")
    assert not upcli.looks_like_takeout(str(tmp_path / "plain"))


def test_takeout_refuses_delete_source(tmp_path, capsys):
    assert upcli.bulk_upload(str(tmp_path), "http://127.0.0.1:9", 1, 1, 0, False, mode="sync",
                             delete_source=True, takeout=True) == 2
    assert "--delete-source" in capsys.readouterr().out


def test_takeout_against_live_server(app, client, tmp_path):
    from modules.encoding import convert as C
    saved = C.media_prefs()
    C.set_media_prefs(dict(saved, image={"target": ".png", "mode": "none"}))
    srv = make_server("127.0.0.1", 0, app.app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        src = tmp_path / "tk"
        src.mkdir()
        write_takeout(str(src), seed0=8100)
        _unzip_second(str(src))
        url = "http://127.0.0.1:%d" % srv.server_port
        assert upcli.bulk_upload(str(src), url, 2, 2, 0.05, False, mode="sync", takeout=True) == 0

        def row(name):
            return app._db().execute("SELECT rel_path, d_original FROM files WHERE rel_path LIKE ?",
                                     ("google-photos/%/" + name,)).fetchone()
        beach = row("beach.png")
        assert beach and beach["rel_path"] == "google-photos/2019/beach.png" and beach["d_original"] == "2019-07-20"
        m = read_meta(client, beach["rel_path"])
        assert "favorite" in m["tags"] and "people:Mom" in m["tags"] and m["description"] == "first day"
        assert "Beach trip" in app._file_albums(beach["rel_path"])
        xmp = open(os.path.join(app.MEDIA_DIR, "google-photos/2019/beach.xmp")).read()
        assert "37,46.272" in xmp and "122,27.402" in xmp
        fam = row("family.png")
        assert "archived" in read_meta(client, fam["rel_path"])["tags"]
        assert "Beach trip" in app._file_albums(fam["rel_path"])
        assert app._db().execute("SELECT COUNT(*) FROM files WHERE rel_path LIKE 'google-photos/%family%'"
                                 ).fetchone()[0] == 1
        assert row(LONG + ".png") is not None
        assert row("cake-edited.png")["d_original"] == "2019-07-22"
        assert row("gone.png") is None

        # a second run sends nothing new
        assert upcli.bulk_upload(str(src), url, 2, 2, 0.05, False, mode="sync", takeout=True) == 0
        assert app._db().execute("SELECT COUNT(*) FROM files WHERE rel_path LIKE 'google-photos/%family%'"
                                 ).fetchone()[0] == 1
    finally:
        srv.shutdown()
        C.set_media_prefs(saved)
        _clear(app, client, "google-photos")


def test_own_exif_date_wins_over_the_sidecar(app, client, tmp_path):
    """! @brief xmp_fallback fills only what the file lacks: a JPEG that says when it was
    taken keeps its own date; the sidecar's position still lands."""
    import io
    from PIL import Image
    img = Image.new("RGB", (40, 30), (10, 120, 200))
    exif = Image.Exif()
    exif[0x8769] = {0x9003: "2015:03:04 05:06:07"}
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    from modules.encoding import convert as C
    saved = C.media_prefs()
    C.set_media_prefs(dict(saved, image={"target": ".png", "mode": "none"}))
    try:
        meta = {"tags": [], "description": "", "regions": [],
                "xmp_fallback": {"exif:DateTimeOriginal": "2019-07-20T11:03:45Z",
                                 "exif:GPSLatitude": "37,46.272000N", "exif:GPSLongitude": "122,27.402000W"}}
        r = client.post("/api/upload", data={"file": (io.BytesIO(buf.getvalue()), "own_exif.jpg"),
                                             "folder": "tkexif", "mode": "sync", "metadata": json.dumps(meta)},
                        content_type="multipart/form-data")
        assert r.status_code == 200, r.get_json()
        rel = r.get_json()["filename"]
        d = app._db().execute("SELECT d_original FROM files WHERE rel_path=?", (rel,)).fetchone()["d_original"]
        assert d == "2015-03-04"
        xmp = open(os.path.join(app.MEDIA_DIR, os.path.splitext(rel)[0] + ".xmp")).read()
        assert "37,46.272" in xmp and "2019-07-20" not in xmp
    finally:
        C.set_media_prefs(saved)
        _clear(app, client, "tkexif")
