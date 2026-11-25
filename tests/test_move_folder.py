"""! @file
@brief Moving a whole folder: every file lands at the same place under the new path,
library files keep their rows, sidecars and album membership, other files move as
they are, and nothing is overwritten."""
import os

from cimtest import media_path


def _row(app, rel):
    return app._db().execute("SELECT rel_path FROM files WHERE rel_path=?", (rel,)).fetchone()


def _clear(app, client, *folders):
    """! @brief Remove what a test made, so later tests see the library as it was."""
    import shutil
    db = app._db()
    for folder in folders:
        for r in db.execute("SELECT rel_path FROM files WHERE rel_path LIKE ?", (folder + "/%",)).fetchall():
            client.post("/api/delete", json={"filename": r["rel_path"], "permanent": True})
        try:
            db.execute("DELETE FROM comics WHERE folder=? OR folder LIKE ?", (folder, folder + "/%"))
            db.commit()
        except Exception:
            pass                                    # no comics module
        shutil.rmtree(media_path(folder), ignore_errors=True)


def _keep_uploads(app):
    from modules.encoding import convert as C
    saved = C.media_prefs()
    C.set_media_prefs(dict(saved, image={"target": ".png", "mode": "none"}))
    return lambda: C.set_media_prefs(saved)


def test_a_folder_moves_with_everything_in_it(app, client, upload):
    restore = _keep_uploads(app)
    try:
        a = upload("a.png", seed=301, folder="mvf_src")
        b = upload("b.png", seed=302, folder="mvf_src/inner")
        with open(media_path("mvf_src/notes.md"), "w") as f:
            f.write("not a library file")
        with open(media_path(os.path.splitext(a)[0] + ".xmp"), "w") as f:
            f.write("<x:xmpmeta xmlns:x='adobe:ns:meta/'/>")
        client.post("/api/albums/create", json={"name": "MvfAlbum"})
        assert client.post("/api/albums/add", json={"album": "MvfAlbum", "files": [a]}).get_json()["added"] == 1

        j = client.post("/api/move_folder", json={"folder": "mvf_src", "new_folder": "mvf_dst/moved"}).get_json()
        assert j["success"] and not j["skipped"], j
        na, nb = "mvf_dst/moved/" + a.split("/", 1)[1], "mvf_dst/moved/" + b.split("/", 1)[1]
        assert _row(app, na) and _row(app, nb) and not _row(app, a) and not _row(app, b)
        assert os.path.exists(media_path(os.path.splitext(na)[0] + ".xmp"))      # sidecar came along
        assert os.path.exists(media_path("mvf_dst/moved/notes.md"))              # other files too
        assert not os.path.exists(media_path("mvf_src"))                          # emptied folder gone
        members = app._db().execute("SELECT rel_path FROM album_members WHERE album='MvfAlbum'").fetchall()
        assert [r[0] for r in members] == [na]
    finally:
        restore()
        _clear(app, client, "mvf_dst", "mvf_src")


def test_a_folder_move_never_overwrites_and_never_moves_into_itself(app, client, upload):
    restore = _keep_uploads(app)
    try:
        a = upload("same.png", seed=311, folder="mvf_c_src")
        upload("same.png", seed=312, folder="mvf_c_dst")
        j = client.post("/api/move_folder", json={"folder": "mvf_c_src", "new_folder": "mvf_c_dst"}).get_json()
        assert j["success"] and [s["rel_path"] for s in j["skipped"]] == [a], j
        assert _row(app, a) and os.path.exists(media_path(a))                      # left in place
        j = client.post("/api/move_folder", json={"folder": "mvf_c_src", "new_folder": "mvf_c_src/deeper"}).get_json()
        assert not j["success"] and "itself" in j["error"]
        assert not client.post("/api/move_folder", json={"folder": "nope_not_here", "new_folder": "x"}).get_json()["success"]
        assert not client.post("/api/move_folder", json={"folder": "mvf_c_src", "new_folder": ".cim/x"}).get_json()["success"]
    finally:
        restore()
        _clear(app, client, "mvf_c_dst", "mvf_c_src")


def test_a_folder_comic_moves_with_its_folder(app, client, upload, host):
    if host.get_service("comic_pages") is None:
        import pytest
        pytest.skip("comics module off")
    restore = _keep_uploads(app)
    try:
        upload("p1.png", seed=321, folder="mvf_comic/issue1")
        upload("p2.png", seed=322, folder="mvf_comic/issue1")
        assert client.post("/api/comic_create", json={"folder": "mvf_comic/issue1", "title": "Issue 1"}).get_json()["success"]
        j = client.post("/api/move_folder", json={"folder": "mvf_comic", "new_folder": "mvf_comics_shelf"}).get_json()
        assert j["success"] and not j["skipped"], j
        rows = [r[0] for r in app._db().execute("SELECT folder FROM comics WHERE folder LIKE 'mvf_comic%'")]
        assert rows == ["mvf_comics_shelf/issue1"]
        page = app._db().execute("SELECT comic_folder FROM files WHERE rel_path LIKE 'mvf_comics_shelf/issue1/%'").fetchone()
        assert page and page[0] == "mvf_comics_shelf/issue1"
    finally:
        restore()
        _clear(app, client, "mvf_comic", "mvf_comics_shelf")
