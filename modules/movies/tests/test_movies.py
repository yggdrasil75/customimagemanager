"""! @file
@brief Movies module: the Plex name parser (pure), the section's indexing from
file names, .nfo files and sidecars, the core events that keep the table right,
/api/movies and the file enricher."""
import io
import os
import shutil
import subprocess

import pytest

from modules.movies import movie_import


# -- plex.py: pure ---------------------------------------------------------
def test_parse_title_year():
    assert plex.parse_title_year("Heat (1995)") == ("Heat", 1995, "")
    assert plex.parse_title_year("Heat (1995) - 1080p") == ("Heat", 1995, "1080p")
    assert plex.parse_title_year("Heat") == ("Heat", None, "")
    assert plex.parse_title_year("Blade Runner (1982) {edition-Final Cut} [imdb-tt0083658]") == ("Blade Runner", 1982, "")
    assert plex.edition_of("Blade Runner (1982) {edition-Final Cut}") == "Final Cut"
    assert plex.sort_title("The Thing") == "thing" and plex.sort_title("An Angel") == "angel"


@pytest.mark.parametrize("path, want", [
    ("Heat (1995)/Heat (1995).mkv", dict(title="Heat", year=1995, folder="Heat (1995)", part=None, extra=None)),
    ("Heat (1995).mkv", dict(title="Heat", year=1995, folder="", part=None, extra=None)),
    ("Heat (1995)/Heat (1995) - pt2.mkv", dict(title="Heat", year=1995, part=2, extra=None)),
    ("Heat (1995) - cd1.mkv", dict(title="Heat", year=1995, folder="", part=1)),
    ("Heat (1995)/Heat (1995)-trailer.mkv", dict(title="Heat", extra="trailer", label="trailer")),
    ("Heat (1995)/Trailers/Teaser.mp4", dict(title="Heat", year=1995, extra="trailer", label="Teaser")),
    ("Heat (1995)/Behind The Scenes/Making of.mkv", dict(extra="behindthescenes", label="Making of")),
    ("Heat (1995)/Heat (1995) - 1080p.mkv", dict(label="1080p", part=None, extra=None)),
    ("The Thing (1982) {edition-Directors Cut}/The Thing (1982).mkv",
     dict(title="The Thing", year=1982, edition="Directors Cut")),
    ("Untitled/clip.mkv", dict(title="Untitled", year=None, folder="Untitled")),
])
def test_parse_movie(path, want):
    got = plex.parse_movie(path)
    assert got, path
    for k, v in want.items():
        assert got[k] == v, (path, k, got)


def test_parse_movie_rejects_what_plex_ignores():
    assert plex.parse_movie("a/b/c/d.mkv") is None
    assert plex.parse_movie("") is None
    assert plex.parse_movie(".hidden/x.mkv") is None


def test_sidecars_in_a_movie_folder():
    names = ["Heat (1995).mkv", "poster.jpg", "fanart.jxl", "Heat (1995).nfo", "Heat (1995).en.srt",
             "Heat (1995).en.forced.srt", "Heat (1995).srt", "Heat (1995)-trailer.en.srt", "notes.txt"]
    sc = plex.sidecars(names, "Heat (1995)")
    assert sc["poster"] == "poster.jpg" and sc["fanart"] == "fanart.jxl" and sc["nfo"] == "Heat (1995).nfo"
    assert [s["file"] for s in sc["subtitles"]] == ["Heat (1995).en.forced.srt", "Heat (1995).en.srt", "Heat (1995).srt"]
    assert sc["subtitles"][0] == {"file": "Heat (1995).en.forced.srt", "lang": "en", "forced": True, "sdh": False}
    assert sc["subtitles"][2]["lang"] == ""
    # the stem-prefixed names win over the generic ones; movie.nfo / any .nfo are fallbacks
    sc = plex.sidecars(["poster.jpg", "Heat (1995)-poster.png", "movie.nfo"], "Heat (1995)")
    assert sc["poster"] == "Heat (1995)-poster.png" and sc["nfo"] == "movie.nfo"
    assert plex.sidecars(["other.nfo"], "Heat (1995)")["nfo"] == "other.nfo"


def test_sidecars_of_a_loose_movie_ignore_the_folder_level_names():
    sc = plex.sidecars(["Heat (1995).mkv", "poster.jpg", "Heat (1995)-poster.jpg", "movie.nfo", "Alien (1979).nfo"],
                       "Heat (1995)", in_folder=False)
    assert sc["poster"] == "Heat (1995)-poster.jpg" and sc["nfo"] is None and sc["fanart"] is None


def test_parse_nfo():
    nfo = plex.parse_nfo('<?xml version="1.0" encoding="UTF-8"?>\n<movie><title>Heat</title><year>1995</year>'
                         '<plot>Cops and robbers.</plot><genre>Crime</genre><genre>Drama</genre>'
                         '<sorttitle>heat</sorttitle></movie>\nhttps://www.imdb.com/title/tt0113277/')
    assert nfo["root"] == "movie" and nfo["title"] == "Heat" and nfo["year"] == 1995
    assert nfo["plot"] == "Cops and robbers." and nfo["genres"] == ["Crime", "Drama"] and nfo["sorttitle"] == "heat"
    # the year falls back to the premiere date; an episode nfo carries season / episode
    ep = plex.parse_nfo("<episodedetails><title>Pilot</title><season>1</season><episode>1</episode>"
                        "<aired>2001-05-01</aired></episodedetails><episodedetails><title>Two</title></episodedetails>")
    assert (ep["root"], ep["title"], ep["season"], ep["episode"], ep["year"]) == ("episodedetails", "Pilot", 1, 1, 2001)
    # malformed XML: the tag scan still reads the fields
    bad = plex.parse_nfo("<movie><title>Bad & broken</title><year>2000</year><genre>Horror</genre></movie>")
    assert bad["title"] == "Bad & broken" and bad["year"] == 2000 and bad["genres"] == ["Horror"]
    assert plex.parse_nfo("") == {} and plex.parse_nfo("just text") == {}
    assert plex.parse_nfo("<tvshow><title>X</title></tvshow>", roots=("movie",)) == {}


def test_srt_to_vtt():
    vtt = plex.srt_to_vtt("1\r\n00:00:01,000 --> 00:00:02,500\r\nHi\r\n\r\n2\r\n00:00:03,000 --> 00:00:04,000\r\nThere\r\n")
    assert vtt.startswith("WEBVTT\n\n00:00:01.000 --> 00:00:02.500\nHi\n\n00:00:03.000")
    assert plex.srt_to_vtt("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nx").startswith("WEBVTT")


# -- the module -------------------------------------------------------------
def _write(app, rel, data=b""):
    ap = os.path.join(app.MEDIA_DIR, rel)
    os.makedirs(os.path.dirname(ap), exist_ok=True)
    with open(ap, "wb") as f:
        f.write(data)
    return ap


def _row(app, rel):
    return app.module_host.db().execute("SELECT * FROM movies WHERE rel_path=?", (rel,)).fetchone()


@pytest.fixture
def tree(app):
    """! @brief A movies tree on disk, dropped with its rows afterwards."""
    yield
    db = app.module_host.db()
    db.execute("DELETE FROM movies WHERE rel_path LIKE 'movies/%' OR rel_path LIKE 'users/%' "
               "OR rel_path LIKE 'loose/%'")
    db.commit()
    for d in ("movies", "loose", os.path.join("users", "ann")):
        shutil.rmtree(os.path.join(app.MEDIA_DIR, d), ignore_errors=True)


def _index(host, app, rel):
    ap = _write(app, rel, b"\x00" * 64)
    assert host.emit("file.index", rel_path=rel, abs_path=ap, force=True) == [], \
        "a video is indexed by the core too: the movies module never claims it"
    return ap


def test_movies_section_is_registered(host):
    sec = host.library_sections()["movies"]
    assert sec["label"] == "Movies" and sec["kinds"] == {"video", "image", "book"} and sec["per_user"]


def test_video_is_a_movie_only_inside_the_section(app, host, tree):
    inside = "movies/Heat (1995)/Heat (1995).mkv"
    personal = "users/ann/movies/Heat (1995).mkv"
    outside = "loose/Heat (1995).mkv"
    for rel in (inside, personal, outside):
        _index(host, app, rel)
    r = _row(app, inside)
    assert r and r["title"] == "Heat" and r["year"] == 1995 and r["folder"] == "movies/Heat (1995)" \
        and r["base"] == "movies"
    p = _row(app, personal)
    assert p and p["folder"] == "" and p["base"] == "users/ann/movies"
    assert _row(app, outside) is None
    # an image in the section is never a movie row
    ap = _write(app, "movies/Heat (1995)/poster.jpg", b"\xff\xd8")
    assert host.emit("file.index", rel_path="movies/Heat (1995)/poster.jpg", abs_path=ap, force=True) == []
    assert _row(app, "movies/Heat (1995)/poster.jpg") is None


def test_nfo_and_sidecars_are_attached(app, host, tree):
    folder = "movies/Heat (1995)"
    _write(app, f"{folder}/poster.jpg", b"\xff\xd8")
    _write(app, f"{folder}/fanart.png", b"\x89PNG")
    _write(app, f"{folder}/Heat (1995).en.srt", b"1\n00:00:01,000 --> 00:00:02,000\nHi\n")
    _write(app, f"{folder}/Heat (1995).de.forced.srt", b"")
    _write(app, f"{folder}/Heat (1995).nfo",
           b"<movie><title>Heat</title><year>1995</year><plot>Cops.</plot><genre>Crime</genre></movie>\nhttps://x/")
    rel = f"{folder}/Heat (1995).mkv"
    _index(host, app, rel)
    r = _row(app, rel)
    assert r["poster_rel"] == f"{folder}/poster.jpg" and r["fanart_rel"] == f"{folder}/fanart.png"
    assert r["nfo_rel"] == f"{folder}/Heat (1995).nfo" and r["plot"] == "Cops." and plex.loads(r["genres"]) == ["Crime"]
    subs = plex.loads(r["subtitles"])
    assert [(s["rel"], s["lang"], s["forced"]) for s in subs] == [
        (f"{folder}/Heat (1995).de.forced.srt", "de", True), (f"{folder}/Heat (1995).en.srt", "en", False)]
    # the nfo overrides the file name's title
    _write(app, f"{folder}/Heat (1995).nfo", b"<movie><title>Heat: Director's Cut</title></movie>")
    _index(host, app, rel)
    assert _row(app, rel)["title"] == "Heat: Director's Cut"


def test_parts_and_extras_group_under_the_movie(app, host, tree, client):
    folder = "movies/Heat (1995)"
    for rel in (f"{folder}/Heat (1995) - pt1.mkv", f"{folder}/Heat (1995) - pt2.mkv",
                f"{folder}/Trailers/Teaser.mp4", f"{folder}/Heat (1995)-trailer.mkv",
                "movies/Alien (1979)/Alien (1979).mkv"):
        _index(host, app, rel)
    j = client.get("/api/movies").get_json()
    assert j["success"]
    by = {m["title"]: m for m in j["movies"]}
    assert set(by) == {"Alien", "Heat"} and j["total"] == 2
    assert [p["part"] for p in by["Heat"]["parts"]] == [1, 2]
    assert by["Heat"]["rel_path"] == f"{folder}/Heat (1995) - pt1.mkv"
    one = client.get("/api/movies/item", query_string={"rel_path": f"{folder}/Heat (1995) - pt2.mkv"}).get_json()
    assert one["success"] and one["movie"]["title"] == "Heat" and len(one["movie"]["parts"]) == 2
    assert sorted(x["label"] for x in one["movie"]["extras"]) == ["Teaser", "trailer"]
    assert client.get("/api/movies/item", query_string={"rel_path": "nope.mkv"}).status_code == 404
    # filters and sorting
    assert [m["title"] for m in client.get("/api/movies?sort=year").get_json()["movies"]] == ["Alien", "Heat"]
    assert [m["title"] for m in client.get("/api/movies?sort=year&dir=desc").get_json()["movies"]] == ["Heat", "Alien"]
    assert [m["title"] for m in client.get("/api/movies?q=ali").get_json()["movies"]] == ["Alien"]
    assert client.get("/api/movies?year=1979").get_json()["total"] == 1


def test_rename_delete_and_sync(app, host, tree):
    inside, outside = "movies/Heat (1995)/Heat (1995).mkv", "loose/Heat (1995).mkv"
    ap = _index(host, app, inside)
    os.makedirs(os.path.join(app.MEDIA_DIR, "loose"), exist_ok=True)
    os.replace(ap, os.path.join(app.MEDIA_DIR, outside))
    host.emit("file.renamed", old_rel=inside, new_rel=outside)
    assert _row(app, inside) is None and _row(app, outside) is None
    os.makedirs(os.path.dirname(ap), exist_ok=True)
    os.replace(os.path.join(app.MEDIA_DIR, outside), ap)
    host.emit("file.renamed", old_rel=outside, new_rel=inside)
    assert _row(app, inside)
    host.emit("file.deleted", rel_path=inside)
    assert _row(app, inside) is None
    # a full pull walks the folders: the file is found again, a stale row goes
    svc = host.get_service("movies")
    host.update_file("movies/Gone (2000)/Gone (2000).mkv", table="movies", dont_write=True,
                     set={"title": "Gone", "base": "movies", "folder": "movies/Gone (2000)"})
    host.emit("library.sync", direction="pull", rel_paths=None)
    assert _row(app, inside) and _row(app, "movies/Gone (2000)/Gone (2000).mkv") is None
    assert svc["state"]["status"] == "idle"


def test_enricher_and_status(app, host, tree, client):
    rel = "movies/Heat (1995)/Heat (1995).mkv"
    _index(host, app, rel)
    rows = [{"filename": rel}, {"filename": "other.jxl"}]
    host.enrich_file_rows(host.db(), rows)
    assert rows[0]["movie_title"] == "Heat" and rows[0]["movie_year"] == 1995 and "movie_title" not in rows[1]
    st = client.get("/api/movies/status").get_json()
    assert st["success"] and st["movies"] >= 1 and st["view"]["rows"]
    assert client.get("/api/movies/genres").get_json()["success"]
    r = client.post("/api/movies/reindex", json={})
    assert r.status_code == 200 and r.get_json()["success"]


def test_subtitle_route_serves_vtt(app, host, tree, client):
    folder = "movies/Heat (1995)"
    _write(app, f"{folder}/Heat (1995).en.srt", b"1\n00:00:01,000 --> 00:00:02,000\nHi\n")
    r = client.get(f"/api/movies/subtitle/{folder}/Heat (1995).en.srt")
    assert r.status_code == 200 and r.mimetype == "text/vtt" and b"00:00:01.000 --> 00:00:02.000" in r.data
    assert client.get(f"/api/movies/subtitle/{folder}/Heat (1995).mkv").status_code == 404
    _write(app, "loose/x.srt", b"")
    assert client.get("/api/movies/subtitle/loose/x.srt").status_code == 404


def test_upload_into_the_section_is_indexed(app, upload, client, tree):
    """! @brief A real upload (a one-second clip) into movies/<Title (Year)>/ shows up in
    /api/movies; an audio file beside it is accepted too (nothing is refused inside a
    section) and stored as audio, never as a movie."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    clip = os.path.join(app.MEDIA_DIR, "..", "clip_movies_test.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", clip], check=True)
    try:
        rel = upload.file(clip, as_name="Clip (2020).mp4", folder="movies/Clip (2020)")
    finally:
        os.remove(clip)
    assert rel.startswith("movies/Clip (2020)/")
    titles = [m["title"] for m in client.get("/api/movies").get_json()["movies"]]
    assert "Clip" in titles
    r = client.post("/api/upload", data={"file": (io.BytesIO(b"\x00" * 16), "song.mp3"),
                                         "mode": "sync", "folder": "movies/Clip (2020)"},
                    content_type="multipart/form-data")
    assert r.status_code == 200, r.get_data(as_text=True)
    j = r.get_json()
    assert j["success"] and j["filename"].startswith("movies/Clip (2020)/song.")
    assert os.path.isfile(os.path.join(app.MEDIA_DIR, j["filename"]))
    assert app.mt.kind(j["filename"]) == "audio"
    assert app.module_host.db().execute("SELECT 1 FROM movies WHERE rel_path=?", (j["filename"],)).fetchone() is None
