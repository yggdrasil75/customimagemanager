"""! @file
@brief Library sections: host.register_library_section, the core's path / folder /
SQL helpers (host.core.library_section, section_folders, section_clause), the one
gallery filter hiding every section's rows, uploads into a section (nothing is
refused; unknown types are stored as sent), and GET /api/sections.

The music module registers `music`; the tests register a throw-away section of
their own (`testsec`) and remove it afterwards."""
import hashlib
import io
import os
import sqlite3

import pytest

from cimtest import png_bytes


@pytest.fixture
def testsec(app, host):
    """! @brief A section of this test's own: images only, public and per user."""
    rec = host.register_library_section("testsec", label="Test section", kinds={"image"})
    yield rec
    app.mt.unregister_library_section("testsec")
    host._refresh_section_filter()


def _rows(sql, params, rows):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE files(rel_path TEXT)")
    db.executemany("INSERT INTO files VALUES(?)", [(r,) for r in rows])
    return [r[0] for r in db.execute(f"SELECT rel_path FROM files WHERE {sql} ORDER BY rowid", params)]


def test_register_validates_and_lists(host, testsec):
    assert testsec["id"] == "testsec" and testsec["kinds"] == {"image"} and testsec["per_user"]
    assert host.library_sections()["testsec"]["label"] == "Test section"
    with pytest.raises(ValueError):
        host.register_library_section("Bad Name", label="x", kinds=set())
    with pytest.raises(ValueError):
        host.register_library_section("users", label="x", kinds=set())


def test_library_section_paths(host, testsec):
    ls = host.core.library_section
    assert ls("testsec/Artist/Album/01.flac") == ("testsec", None, "Artist/Album/01.flac")
    assert ls("TestSec/x.jpg") == ("testsec", None, "x.jpg")          # case-folding filesystems
    assert ls("testsec") == ("testsec", None, "")
    assert ls("users/ann/testsec/A/1.jpg") == ("testsec", "ann", "A/1.jpg")
    assert ls("users/ann/testsec") == ("testsec", "ann", "")
    assert ls("users/ann/other/testsec/1.jpg") == (None, None, None)
    assert ls("testsecx/1.jpg") == (None, None, None)
    assert ls("a/testsec/1.jpg") == (None, None, None)
    assert ls("pic.jpg") == (None, None, None)
    assert ls("") == (None, None, None)


def test_library_section_public_only(app, host):
    app.mt.register_library_section("pubonly", label="Public", kinds={"video"}, per_user=False)
    try:
        assert host.core.library_section("pubonly/a.mkv") == ("pubonly", None, "a.mkv")
        assert host.core.library_section("users/ann/pubonly/a.mkv") == (None, None, None)
        sql, p = host.core.section_clause("pubonly")
        assert _rows(sql, p, ["pubonly/a.mkv", "users/ann/pubonly/a.mkv", "x.jpg"]) == ["pubonly/a.mkv"]
    finally:
        app.mt.unregister_library_section("pubonly")


def test_section_folders_on_disk(app, host, testsec):
    media = app.MEDIA_DIR
    assert host.core.section_folders("testsec") == []
    made = [os.path.join(media, "testsec"), os.path.join(media, "users", "zed", "testsec"),
            os.path.join(media, "users", "amy", "testsec"), os.path.join(media, "users", "amy", "other")]
    for d in made:
        os.makedirs(d, exist_ok=True)
    try:
        assert host.core.section_folders("testsec") == ["testsec", "users/amy/testsec", "users/zed/testsec"]
        assert host.core.section_folders("nosuch") == []
        folders = {s["id"]: s for s in host.app.test_client().get("/api/sections").get_json()["sections"]}
        assert folders["testsec"]["folders"] == ["testsec", "users/amy/testsec", "users/zed/testsec"]
    finally:
        for d in reversed(made):
            os.rmdir(d)


def test_section_clause_matches_exact_segments(host, testsec):
    rows = ["testsec/A/1.jpg", "TESTSEC/2.jpg", "testsecx/3.jpg", "users/ann/testsec/4.jpg",
            "users/ann/other/testsec/5.jpg", "users/testsec", "a/testsec/6.jpg", "pic.jpg"]
    sql, p = host.core.section_clause("testsec")
    assert _rows(sql, p, rows) == ["testsec/A/1.jpg", "TESTSEC/2.jpg", "users/ann/testsec/4.jpg"]
    sql, p = host.core.section_clause("testsec", column="f.rel_path")
    assert "f.rel_path" in sql
    sql, p = host.core.section_clause("nosuch")
    assert _rows(sql, p, rows) == []


def test_one_gallery_filter_hides_every_section(app, host, testsec):
    filters = [f for f in host.gallery_filters if "testsec/" in f]
    assert len(filters) == 1 and filters[0] is host._section_filter
    if "music" in host.library_sections():
        assert "music/" in filters[0], "the music module's section shares the one filter"
    rows = ["testsec/A/1.jpg", "users/ann/testsec/2.jpg", "music/B/3.flac", "testsecx/4.jpg", "pic.jpg"]
    assert _rows(filters[0], [], rows) == ["testsecx/4.jpg", "pic.jpg"]
    # registering another section rebuilds the filter in place, never adds a second one
    n = len(host.gallery_filters)
    app.mt.register_library_section("another", label="A", kinds=set())
    try:
        host.register_library_section("another", label="A", kinds=set())
        assert len(host.gallery_filters) == n and "another/" in host._section_filter
    finally:
        app.mt.unregister_library_section("another")


def test_section_rows_stay_out_of_the_gallery(client, upload, testsec):
    inside = upload("art.png", seed=71, folder="testsec/Band/Album")
    outside = upload("free.png", seed=72, folder="elsewhere")
    assert inside.startswith("testsec/Band/Album/")
    names = {f["filename"] for f in client.get("/api/list", query_string={"per_page": 500}).get_json()["files"]}
    assert outside in names and inside not in names
    names = {f["filename"] for f in client.get("/api/list", query_string={"folder": "/", "recursive": "1",
                                                                            "per_page": 500}).get_json()["files"]}
    assert inside not in names
    folders = {f["path"] for f in client.get("/api/folders").get_json()["folders"]}
    assert "elsewhere" in folders and "testsec/Band/Album" not in folders
    # the file itself is indexed and served as usual
    assert client.get(f"/api/file/{inside}").status_code == 200


def _post(client, name, folder, data):
    return client.post("/api/upload", data={"file": (io.BytesIO(data), name), "mode": "sync", "folder": folder},
                       content_type="multipart/form-data")


def test_upload_into_a_section_refuses_nothing(client, host, app, upload, testsec):
    """! @brief `kinds` is what the module indexes, not a gate: a video goes into an
    images-only section, an mp3 into movies/X (2000); a file of a type the app does not
    handle ("notes.weird") is stored as sent, with its sha256 in the receipt."""
    stored = []
    try:
        r = _post(client, "clip.mp4", "testsec/Band", b"testsec-clip-" + b"\x00" * 51)   # unique bytes: not a duplicate of another test's clip
        assert r.status_code == 200, r.get_data(as_text=True)
        j = r.get_json()
        assert j["success"] and j["filename"].startswith("testsec/Band/clip.")
        assert host.core.library_section(j["filename"])[0] == "testsec"
        stored.append(j["filename"])
        # the chunked path opens a session for it too
        r = client.post("/api/upload/session", json={"filename": "clip.mp4", "size": 64, "folder": "users/ann/testsec"})
        assert r.status_code == 200 and r.get_json()["success"]
        if "movies" in host.library_sections():
            r = _post(client, "song.mp3", "movies/X (2000)", b"\x00" * 16)
            assert r.status_code == 200, r.get_data(as_text=True)
            j = r.get_json()
            assert j["success"] and j["filename"].startswith("movies/X (2000)/song.")
            assert os.path.isfile(os.path.join(app.MEDIA_DIR, j["filename"]))
            stored.append(j["filename"])
            r = _post(client, "notes.weird", "movies/X (2000)", b"nobody handles this")
            assert r.status_code == 200, r.get_data(as_text=True)
            j = r.get_json()
            assert j["success"] and j["sidecar"] and j["filename"] == "movies/X (2000)/notes.weird"
            assert j["sha256"] == hashlib.sha256(b"nobody handles this").hexdigest()
            with open(os.path.join(app.MEDIA_DIR, j["filename"]), "rb") as fh:
                assert fh.read() == b"nobody handles this"
            stored.append(j["filename"])
        # the kinds it indexes go through as ever; outside a section nothing changed
        assert upload("ok.png", seed=73, folder="testsec/Band").startswith("testsec/Band/")
        assert host.core.library_section(upload("ok2.png", seed=74, folder="free"))[0] is None
        r = _post(client, "notes.weird", "free", b"nobody handles this")
        assert r.status_code == 422 and r.get_json()["error_code"] == "conversion_failed"
    finally:
        for fn in stored:
            client.post("/api/delete", json={"filename": fn, "permanent": True})


def test_api_sections(client, host, testsec):
    j = client.get("/api/sections").get_json()
    assert j["success"]
    by = {s["id"]: s for s in j["sections"]}
    assert set(by["testsec"]) >= {"id", "label", "kinds", "folders", "sidecars"}
    assert by["testsec"]["label"] == "Test section" and by["testsec"]["kinds"] == ["image"]
    assert isinstance(by["testsec"]["folders"], list) and by["testsec"]["sidecars"] == []
    if "music" in host.library_sections():
        assert by["music"]["kinds"] == ["audio", "book", "image", "video"] and by["music"]["label"] == "Music"
        assert by["music"]["sidecars"] == [".cue", ".log", ".m3u", ".m3u8", ".nfo"]
    if "movies" in host.library_sections():
        assert by["movies"]["sidecars"] == [".ass", ".idx", ".nfo", ".srt", ".ssa", ".sub", ".sup", ".vtt"]


def test_section_sidecars_are_stored_verbatim_and_names_keep_plex_form(client, app):
    """! @brief A movie's .nfo / .srt uploaded into its section folder is stored as sent
    (never converted or rejected) and the filename keeps its spaces and brackets."""
    import io
    r = client.post("/api/upload", data={"file": (io.BytesIO(b"<movie><title>T</title></movie>"), "Film (1999).nfo"),
                                         "folder": "movies/Film (1999)", "mode": "sync"},
                    content_type="multipart/form-data").get_json()
    assert r["success"] and r["filename"] == "movies/Film (1999)/Film (1999).nfo" and r.get("sha256")
    again = client.post("/api/upload", data={"file": (io.BytesIO(b"<movie/>"), "Film (1999).nfo"),
                                             "folder": "movies/Film (1999)", "mode": "sync"},
                        content_type="multipart/form-data").get_json()
    assert again["duplicate"] and again["existing_sha256"] == r["sha256"]
    r = client.post("/api/upload", data={"file": (io.BytesIO(b"x"), "Film (1999).nfo"), "folder": "plain", "mode": "sync"},
                    content_type="multipart/form-data")
    assert r.status_code != 200 or not r.get_json().get("success")   # outside a section: as before
