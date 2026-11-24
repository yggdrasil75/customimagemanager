"""! @file
@brief Public libraries: the Plex / Jellyfin names (pure), arranging a public section
with everything that belongs beside an item, publishing a private item, and new files
in a public section arranged on their own.
"""
import json
import os
import shutil
import time

import pytest

from modules.public_library import arrange


# -- arrange.py: pure ----------------------------------------------------------------------
def test_names_plex_and_jellyfin_read():
    assert arrange.movie_path("Heat", 1995, ext=".mkv") == "Heat (1995)/Heat (1995).mkv"
    assert arrange.movie_path("Some.Movie.2020.1080p.WEB", None, ext=".mkv") == \
        "Some Movie (2020)/Some Movie (2020).mkv"
    assert arrange.movie_path("Heat", 1995, part=2, ext=".mkv") == "Heat (1995)/Heat (1995) - pt2.mkv"
    assert arrange.movie_path("Heat", 1995, extra="trailer", label="Teaser", ext=".mp4") == \
        "Heat (1995)/Trailers/Teaser.mp4"
    assert arrange.movie_path("Blade Runner", 1982, edition="Final Cut", ext=".mkv") == \
        "Blade Runner (1982) {edition-Final Cut}/Blade Runner (1982) {edition-Final Cut}.mkv"
    assert arrange.episode_path("Firefly", 2002, 1, 1, title="Serenity: Part 1", ext=".mkv") == \
        "Firefly (2002)/Season 01/Firefly (2002) - S01E01 - Serenity - Part 1.mkv"
    assert arrange.episode_path("Show", None, 2, 3, episode_end=4, ext=".mkv") == \
        "Show/Season 02/Show - S02E03-E04.mkv"
    assert arrange.track_path("Artist", "Album", "Song", track=2, ext=".flac") == "Artist/Album/02 - Song.flac"
    assert arrange.track_path("Artist", "Album", "Song", track=2, disc=2, discs=2, ext=".flac") == \
        "Artist/Album/202 - Song.flac"
    assert arrange.book_path("Ursula K. Le Guin", "The Dispossessed", ".epub") == \
        "Ursula K. Le Guin/The Dispossessed/The Dispossessed.epub"
    assert arrange.track_path("", "Album", "Song") is None and arrange.book_path("A", "") is None
    assert arrange.clean('a<b>:c?"d') == "ab -cd"


def test_loose_episode_names():
    p = arrange.parse_episode("Show.Name.2005.S01E02.The.Title.720p.WEB")
    assert p == {"show": "Show Name", "show_year": 2005, "season": 1, "episode": 2,
                 "episode_end": None, "title": "The Title"}
    assert arrange.parse_episode("Show - s02e03e04 - Two Parter")["episode_end"] == 4
    assert arrange.parse_episode("s01e01") is None and arrange.parse_episode("holiday") is None


# -- the module ----------------------------------------------------------------------------
def _write(app, rel, data=b"\x00" * 64):
    ap = os.path.join(app.MEDIA_DIR, rel)
    os.makedirs(os.path.dirname(ap), exist_ok=True)
    with open(ap, "wb") as f:
        f.write(data)
    return ap


def _index(host, app, rel):
    ap = _write(app, rel)
    host.emit("file.index", rel_path=rel, abs_path=ap, force=True)
    return ap


def _exists(app, rel):
    return os.path.isfile(os.path.join(app.MEDIA_DIR, rel))


@pytest.fixture
def tree(app):
    yield
    db = app.module_host.db()
    for t in ("movies", "tv_episodes", "tv_shows", "music", "books"):
        try:
            db.execute(f"DELETE FROM {t} WHERE rel_path LIKE 'movies/%' OR rel_path LIKE 'tv/%' "
                       "OR rel_path LIKE 'music/%' OR rel_path LIKE 'books/%' OR rel_path LIKE 'users/%'")
        except Exception:
            pass
    db.commit()
    for d in ("movies", "tv", "music", "books", os.path.join("users", "ann")):
        shutil.rmtree(os.path.join(app.MEDIA_DIR, d), ignore_errors=True)


def test_arrange_moves_a_movie_with_its_subtitles_and_art(app, host, tree):
    _index(host, app, "movies/Heat.1995.1080p.BluRay.mkv")
    _write(app, "movies/Heat.1995.1080p.BluRay.en.srt", b"1\n00:00:01,000 --> 00:00:02,000\nHi\n")
    _index(host, app, "movies/Blade.Runner.1982.720p/br.mkv")       # the folder names the movie
    _write(app, "movies/Blade.Runner.1982.720p/poster.jpg", b"\xff\xd8")
    _write(app, "movies/Blade.Runner.1982.720p/movie.nfo", b"<movie/>")
    res = host.get_service("public_library")["arrange"]("movies")
    assert res["moved"] == 2, res
    assert _exists(app, "movies/Heat (1995)/Heat (1995).mkv")
    assert _exists(app, "movies/Heat (1995)/Heat (1995).en.srt")
    assert _exists(app, "movies/Blade Runner (1982)/Blade Runner (1982).mkv")
    # what the old folder held came along, and the folder is gone
    assert _exists(app, "movies/Blade Runner (1982)/poster.jpg") and _exists(app, "movies/Blade Runner (1982)/movie.nfo")
    assert not os.path.exists(os.path.join(app.MEDIA_DIR, "movies/Blade.Runner.1982.720p"))
    # the movies module follows the move
    row = app.module_host.db().execute("SELECT title, year FROM movies WHERE rel_path=?",
                                       ("movies/Heat (1995)/Heat (1995).mkv",)).fetchone()
    assert row and row["title"] == "Heat" and row["year"] == 1995
    # arranging again changes nothing
    assert host.get_service("public_library")["arrange"]("movies")["moved"] == 0


def test_music_album_with_its_folder_files(app, host, tree):
    if host.get_service("music") is None:
        pytest.skip("music module off (needs mutagen and librosa)")
    db = app.module_host.db()
    for n, (t, disc) in enumerate([("One", 1), ("Two", 2)], start=1):
        rel = f"music/Rip 2001/CD{disc}/track{n}.flac"
        _write(app, rel)
        db.execute("INSERT OR REPLACE INTO music (rel_path, title, artist, album, albumartist, track, disc) "
                   "VALUES (?,?,?,?,?,?,?)", (rel, t, "Band", "Record", "", 1, disc))
    db.commit()
    _write(app, "music/Rip 2001/cover.jpg", b"\xff\xd8")
    _write(app, "music/Rip 2001/CD1/rip.log", b"EAC")
    _write(app, "music/Rip 2001/CD1/track1.lrc", b"[00:01]la")
    res = host.get_service("public_library")["arrange"]("music")
    assert res["moved"] == 2, res
    assert _exists(app, "music/Band/Record/101 - One.flac") and _exists(app, "music/Band/Record/201 - Two.flac")
    assert _exists(app, "music/Band/Record/101 - One.lrc")          # lyrics follow the track
    assert _exists(app, "music/Band/Record/cover.jpg") and _exists(app, "music/Band/Record/rip.log")
    assert not os.path.exists(os.path.join(app.MEDIA_DIR, "music/Rip 2001"))


def test_publish_moves_a_private_book_and_a_loose_episode(app, host, tree, client):
    db = app.module_host.db()
    rel = "users/ann/books/download/dispossessed.epub"
    _write(app, rel, b"PK")
    db.execute("INSERT OR REPLACE INTO books (rel_path, title, authors) VALUES (?,?,?)",
               (rel, "The Dispossessed", json.dumps(["Ursula K. Le Guin"])))
    db.commit()
    ep = "users/ann/tv/Show.Name.S01E02.Title.720p.mkv"
    _write(app, ep)
    j = client.post("/api/public_library/publish", json={"filenames": [rel, ep, "loose/x.jpg"]}).get_json()
    assert j["success"] and j["moved"] == 2, j
    assert _exists(app, "books/Ursula K. Le Guin/The Dispossessed/The Dispossessed.epub")
    assert _exists(app, "tv/Show Name/Season 01/Show Name - S01E02 - Title.mkv")
    assert not _exists(app, rel) and not _exists(app, ep)
    assert [s[0] for s in j["skipped"]] == ["loose/x.jpg"]


def test_new_files_in_a_public_section_are_arranged_on_their_own(app, host, tree):
    pl = host.get_service("public_library")
    pl["settle"](0.2)
    pl["pause"](False)
    try:
        _index(host, app, "movies/Heat.1995.mkv")
        host.emit("file.indexed", rel_path="movies/Heat.1995.mkv", abs_path="")
        for _ in range(300):                                      # up to 15 s on a slow runner
            if _exists(app, "movies/Heat (1995)/Heat (1995).mkv"):
                break
            time.sleep(0.05)
        assert _exists(app, "movies/Heat (1995)/Heat (1995).mkv")
        # private folders keep the user's own layout
        _index(host, app, "users/ann/movies/Heat.1995.mkv")
        host.emit("file.indexed", rel_path="users/ann/movies/Heat.1995.mkv", abs_path="")
        time.sleep(0.6)
        assert _exists(app, "users/ann/movies/Heat.1995.mkv")
    finally:
        pl["pause"](True)
        pl["settle"](8.0)


def test_paused_arranging_leaves_a_sync_alone(app, host, tree):
    """While arranging is paused (the test run pauses it), a full sync moves nothing
    either: it used to start arrange_all anyway, renaming other tests' files mid-test."""
    rel = "movies/Heat.1995.mkv"
    _index(host, app, rel)
    host.emit("library.sync", direction="pull", rel_paths=None)
    time.sleep(0.5)
    assert _exists(app, rel) and not _exists(app, "movies/Heat (1995)/Heat (1995).mkv")


def test_publishing_a_video_stored_as_separate_streams_puts_it_back_together(app, host, tree, client):
    import subprocess
    from modules.encoding import demux as D
    if not D.available():
        pytest.skip("needs mkvtoolnix")
    import modules.encoding as enc
    d = os.path.join(app.MEDIA_DIR, "users", "ann", "movies")
    os.makedirs(d, exist_ok=True)
    src = os.path.join(d, "Heat.1995.mkv")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=25",
                    "-f", "lavfi", "-i", "sine=f=440", "-t", "1", "-c:v", "libx264", "-c:a", "aac",
                    "-shortest", src], check=True)
    split = enc._state["demuxer"].demux_rel("users/ann/movies/Heat.1995.mkv")
    assert split.endswith(".avc") and os.path.exists(os.path.join(d, "Heat.1995 - unknown.aac"))
    j = client.post("/api/public_library/publish", json={"filenames": [split]}).get_json()
    assert j["success"] and j["moved"] == 1, j
    assert _exists(app, "movies/Heat (1995)/Heat (1995).mkv")
    assert not os.listdir(d)