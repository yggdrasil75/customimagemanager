"""! @file
@brief The music section: audio counts as music only inside <media>/music or
users/<name>/music, and so does a music video there (still a video for the core:
poster, playback; a track for the Music tab); album art, artist pictures and descriptions come from the folders of
the Plex layout (Artist/Album/track) and ride along in the album / artist replies."""
import io
import os
import shutil
import time

import pytest


def _write(app, rel, data=b""):
    ap = os.path.join(app.MEDIA_DIR, rel)
    os.makedirs(os.path.dirname(ap), exist_ok=True)
    with open(ap, "wb") as f:
        f.write(data)
    return ap


@pytest.fixture
def tree(app):
    """! @brief A section tree on disk and the rows the indexer would have made."""
    made = []
    yield made
    db = app.module_host.db()
    db.execute("DELETE FROM music WHERE rel_path LIKE 'music/%' OR rel_path LIKE 'users/%' "
               "OR rel_path LIKE 'loose/%'")
    db.commit()
    for d in ("music", "loose", os.path.join("users", "ann")):
        shutil.rmtree(os.path.join(app.MEDIA_DIR, d), ignore_errors=True)


def _row(app, rel):
    return app.module_host.db().execute("SELECT rel_path FROM music WHERE rel_path=?", (rel,)).fetchone()


def test_music_section_is_registered(host):
    sec = host.library_sections()["music"]
    assert sec["label"] == "Music" and sec["kinds"] == {"audio", "video", "image", "book"} and sec["per_user"]
    assert sec["sidecars"] == {".cue", ".log", ".m3u", ".m3u8", ".nfo"}


def test_audio_is_music_only_inside_a_section(app, host, tree):
    # a stray .flac (no decodable tags: the row keeps the file name as its title)
    inside = "music/Band/Album/01 - Song.flac"
    personal = "users/ann/music/Band/Album/01 - Song.flac"
    outside = "loose/01 - Song.flac"
    for rel in (inside, personal, outside):
        ap = _write(app, rel, b"fLaC" + b"\x00" * 64)
        handled = host.emit("file.index", rel_path=rel, abs_path=ap, force=True)
        assert handled == ([True] if rel != outside else []), \
            "the music module claims audio inside a music section; outside, the core indexes it"
    assert _row(app, inside) and _row(app, personal)
    assert _row(app, outside) is None, "audio outside a music section is not a track"
    # ...but it is still a library file: the core gives it a row like any other
    app._index_file(outside, force=True)
    assert app._get_file_row(outside) is not None
    # a video inside the section is a track (a music video) and still the core's to index
    clip = "music/Band/Album/live.mp4"
    ap = _write(app, clip, b"\x00" * 64)
    assert host.emit("file.index", rel_path=clip, abs_path=ap, force=True) == []
    assert _row(app, clip) is not None
    # outside a section it is just a video
    loose = "loose/live.mp4"
    ap = _write(app, loose, b"\x00" * 64)
    host.emit("file.index", rel_path=loose, abs_path=ap, force=True)
    assert _row(app, loose) is None


def test_moving_a_track_in_and_out(app, host, tree):
    inside, outside = "music/Band/Album/02.flac", "loose/02.flac"
    ap = _write(app, inside, b"fLaC" + b"\x00" * 64)
    host.emit("file.index", rel_path=inside, abs_path=ap, force=True)
    assert _row(app, inside)
    # out of the section: the row goes
    os.makedirs(os.path.join(app.MEDIA_DIR, "loose"), exist_ok=True)
    os.replace(ap, os.path.join(app.MEDIA_DIR, outside))
    host.emit("file.renamed", old_rel=inside, new_rel=outside)
    assert _row(app, inside) is None and _row(app, outside) is None
    # back in: indexed again
    os.makedirs(os.path.dirname(ap), exist_ok=True)
    os.replace(os.path.join(app.MEDIA_DIR, outside), ap)
    host.emit("file.renamed", old_rel=outside, new_rel=inside)
    assert _row(app, inside)
    # within the sections: the row follows
    moved = "users/ann/music/Band/Album/02.flac"
    _write(app, moved, b"fLaC" + b"\x00" * 64)
    host.emit("file.renamed", old_rel=inside, new_rel=moved)
    assert _row(app, inside) is None and _row(app, moved)


def test_full_walk_covers_only_the_sections(app, host, tree):
    for rel in ("music/A/B/1.flac", "users/ann/music/C/2.flac", "loose/3.flac"):
        _write(app, rel, b"fLaC" + b"\x00" * 64)
    # a row left over from before sections existed
    db = app.module_host.db()
    db.execute("INSERT OR REPLACE INTO music(rel_path, title, mtime) VALUES(?,?,?)", ("loose/old.flac", "x", 1.0))
    db.commit()
    host.get_service("music")["index_all"](force=True)
    assert _row(app, "music/A/B/1.flac") and _row(app, "users/ann/music/C/2.flac")
    assert _row(app, "loose/3.flac") is None and _row(app, "loose/old.flac") is None


def test_album_art_and_descriptions_by_folder(app, client, host, tree):
    db = app.module_host.db()
    rows = [("music/Band/Album/01.mp3", "One", "Band", "Album"), ("music/Band/Album/02.mp3", "Two", "Band", "Album"),
            ("music/Solo/03.mp3", "Three", "Solo", "Single"), ("music/04.mp3", "Four", "Root", "Rootless")]
    for rel, title, artist, album in rows:
        db.execute("INSERT OR REPLACE INTO music(rel_path,title,artist,album,tags,mtime,created) "
                   "VALUES(?,?,?,?,?,?,?)", (rel, title, artist, album, "[]", time.time(), time.time()))
    db.commit()
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    _write(app, "music/Band/Album/anything.png", png)       # any image in the album folder is its art
    _write(app, "music/Band/band.jpg", png)                 # the artist picture
    _write(app, "music/Band/Album/notes.txt", b"Recorded live.")
    _write(app, "music/Band/bio.md", b"# Band\nFormed in 1999.")
    _write(app, "music/Solo/front.jpg", png)                # Artist/track: the one folder serves both

    albums = {a["album"]: a for a in client.get("/api/music/albums").get_json()["albums"]}
    a = albums["Album"]
    assert a["folder"] == "music/Band/Album" and a["art_rel"] == "music/Band/Album/anything.png"
    assert a["tracks"] == 2 and "cover_rel" in a
    if ".txt" in host.media.BOOK_EXTS:
        assert a["description_rel"] == "music/Band/Album/notes.txt" and a["description"] == "Recorded live."
    s = albums["Single"]
    assert s["folder"] == "music/Solo" and s["art_rel"] == "music/Solo/front.jpg"
    r = albums["Rootless"]
    assert r["folder"] is None and r["art_rel"] is None and r["description"] is None

    artists = {a["name"]: a for a in client.get("/api/music/artists").get_json()["artists"]}
    b = artists["Band"]
    assert b["folder"] == "music/Band" and b["picture_rel"] == "music/Band/band.jpg"
    if ".md" in host.media.BOOK_EXTS:
        assert b["description_rel"] == "music/Band/bio.md" and b["description"].startswith("# Band")
    assert artists["Solo"]["picture_rel"] == "music/Solo/front.jpg"
    assert artists["Root"]["folder"] is None and artists["Root"]["picture_rel"] is None

    # the album folder's image also answers the track's cover request
    _write(app, "music/Band/Album/01.mp3", b"\x00" * 64)
    r = client.get("/api/music/cover/music/Band/Album/01.mp3")
    assert r.status_code == 200 and r.data.startswith(b"\x89PNG")


def test_a_music_video_uploaded_into_the_section_is_a_track(app, client, tree):
    """! @brief A music video in an album folder is accepted, kept as a video by the core
    and listed by the Music tab under the song and artist its tags carry (yt-dlp
    writes them), with its own frame as the cover."""
    import subprocess
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    src = os.path.join(app.MEDIA_DIR, "..", "mv_src.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=64x64:d=1",
                    "-f", "lavfi", "-i", "sine=d=1", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-metadata", "title=The Song", "-metadata", "artist=The Band", src],
                   check=True)
    try:
        with open(src, "rb") as f:
            data = f.read()
    finally:
        os.remove(src)
    rel = "music/Band/Album/clip.mp4"
    r = client.post("/api/upload", data={"file": (io.BytesIO(data), "clip.mp4"), "mode": "sync",
                                         "folder": "music/Band/Album"}, content_type="multipart/form-data")
    assert r.status_code == 200, r.get_data(as_text=True)
    j = r.get_json()
    assert j["success"] and j["filename"] == rel and not j.get("sidecar")
    assert os.path.isfile(os.path.join(app.MEDIA_DIR, rel))
    row = app.module_host.db().execute("SELECT media_kind FROM files WHERE rel_path=?", (rel,)).fetchone()
    assert row and row[0] == "video"
    song = None
    for _ in range(30):
        song = next((x for x in client.get("/api/music/songs").get_json().get("songs", [])
                     if x["rel_path"] == rel), None)
        if song:
            break
        time.sleep(0.2)
    assert song, "a music video in the section is listed as a track"
    assert song["title"] == "The Song" and song["artist"] == "The Band" and song["duration"] > 0
    c = client.get("/api/music/cover/" + rel)
    assert c.status_code == 302 and c.headers["Location"].endswith("/api/thumb/" + rel)
    assert client.get("/api/music/stream/" + rel).status_code == 200
    client.post("/api/delete", json={"filename": rel})
    # the chunked path opens a session for it as well
    r = client.post("/api/upload/session", json={"filename": "clip2.mkv", "size": 10, "folder": "music/Band"})
    assert r.status_code == 200 and r.get_json()["success"]