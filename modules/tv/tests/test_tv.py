"""! @file
@brief TV module: the Plex episode parser (pure), the section's indexing of shows
and episodes from file names, .nfo files and sidecars, the core events that keep
the tables right, /api/tv/* and the file enricher."""
import io
import os
import shutil

import pytest

from modules.tv import movie_import


# -- movie_import.py: pure ---------------------------------------------------------
@pytest.mark.parametrize("name, want", [
    ("Season 01", 1), ("Season 1", 1), ("season 12", 12), ("S03", 3), ("Specials", 0), ("Season 00", 0),
    ("Extras", None), ("Show (2001)", None), ("", None),
])
def test_parse_season_folder(name, want):
    assert movie_import.parse_season_folder(name) == want


@pytest.mark.parametrize("path, want", [
    ("Show (2001)/Season 01/Show - s01e01 - Pilot.mkv",
     dict(show="Show", show_year=2001, season=1, episode=1, episode_end=None, title="Pilot", season_folder="Season 01")),
    ("Show (2001)/Specials/Show - s00e01 - Xmas.mkv", dict(season=0, episode=1, title="Xmas")),
    ("Show (2001)/Season 1/Show - S01E01.mkv", dict(season=1, episode=1, title="")),
    ("Show (2001)/Season 01/Show - 1x01 - Pilot.mkv", dict(season=1, episode=1, title="Pilot")),
    ("Show (2001)/Season 01/Show - s01e01-e02 - Two.mkv", dict(season=1, episode=1, episode_end=2, title="Two")),
    ("Show (2001)/Season 01/Show - s01e01e02.mkv", dict(episode=1, episode_end=2)),
    ("Show (2001)/Season 01/Show.S01E03.Some.Title.mkv", dict(season=1, episode=3, title="Some Title")),
    ("Show (2001)/Show - 2x05.mkv", dict(season=2, episode=5, season_folder="")),
    ("Show (2001)/Season 02/Show - 1x05 - Mixed.mkv", dict(season=2, episode=5)),
    ("Show (2001)/Season 01/Trailers/promo.mkv", dict(season=1, episode=None, extra="trailer", title="promo")),
    ("Show (2001)/Extras/bloopers.mkv", dict(season=None, extra="other", title="bloopers")),
    ("Show/Season 01/s01e01.mkv", dict(show="Show", show_year=None, season=1, episode=1)),
])
def test_parse_episode(path, want):
    got = movie_import.parse_episode(path)
    assert got, path
    for k, v in want.items():
        assert got[k] == v, (path, k, got)


def test_parse_episode_rejects_what_plex_ignores():
    assert movie_import.parse_episode("Show (2001)/Season 01/random.mkv") is None      # no episode marker
    assert movie_import.parse_episode("loose.mkv") is None                               # no show folder
    assert movie_import.parse_episode("Show (2001)/Season 01/Deep/x/Show - s01e01.mkv") is None
    assert movie_import.parse_episode("Show (2001)/Season 01/.part/Show - s01e01.mkv") is None


def test_show_and_season_sidecars():
    names = ["poster.jxl", "fanart.jpg", "banner.png", "tvshow.nfo", "season01-poster.jpg",
             "season-specials-poster.png", "Season 01", "notes.txt"]
    sc = movie_import.show_sidecars(names)
    assert sc == {"poster": "poster.jxl", "fanart": "fanart.jpg", "banner": "banner.png", "nfo": "tvshow.nfo"}
    assert movie_import.season_poster(names, 1) == "season01-poster.jpg"
    assert movie_import.season_poster(names, 0) == "season-specials-poster.png"
    assert movie_import.season_poster(names, 2) is None
    ep = movie_import.episode_sidecars(["Show - s01e01 - Pilot.mkv", "Show - s01e01 - Pilot.nfo",
                                "Show - s01e01 - Pilot.en.srt", "Show - s01e01 - Pilot-thumb.jpg",
                                "Show - s01e02 - Two.en.srt"], "Show - s01e01 - Pilot")
    assert ep["thumb"] == "Show - s01e01 - Pilot-thumb.jpg" and ep["nfo"] == "Show - s01e01 - Pilot.nfo"
    assert [s["file"] for s in ep["subtitles"]] == ["Show - s01e01 - Pilot.en.srt"]


def test_parse_nfo_show_and_episode():
    show = movie_import.parse_nfo("<tvshow><title>The Show</title><year>2001</year><plot>About.</plot>"
                          "<genre>Drama</genre><genre>Comedy</genre></tvshow>", roots=("tvshow",))
    assert (show["title"], show["year"], show["plot"], show["genres"]) == ("The Show", 2001, "About.", ["Drama", "Comedy"])
    ep = movie_import.parse_nfo('<?xml version="1.0"?><episodedetails><title>Pilot</title><season>1</season>'
                        '<episode>1</episode><plot>It starts.</plot><aired>2001-05-01</aired></episodedetails>',
                        roots=("episodedetails",))
    assert (ep["title"], ep["season"], ep["episode"], ep["plot"], ep["aired"]) == ("Pilot", 1, 1, "It starts.", "2001-05-01")
    assert movie_import.parse_nfo("<movie><title>x</title></movie>", roots=("tvshow",)) == {}
    assert movie_import.srt_to_vtt("1\n00:00:01,000 --> 00:00:02,000\nHi\n").startswith("WEBVTT\n\n00:00:01.000")


# -- the module -------------------------------------------------------------
def _write(app, rel, data=b""):
    ap = os.path.join(app.MEDIA_DIR, rel)
    os.makedirs(os.path.dirname(ap), exist_ok=True)
    with open(ap, "wb") as f:
        f.write(data)
    return ap


def _ep(app, rel):
    return app.module_host.db().execute("SELECT * FROM tv_episodes WHERE rel_path=?", (rel,)).fetchone()


def _show(app, rel):
    return app.module_host.db().execute("SELECT * FROM tv_shows WHERE rel_path=?", (rel,)).fetchone()


@pytest.fixture
def tree(app):
    """! @brief A tv tree on disk, dropped with its rows afterwards."""
    yield
    db = app.module_host.db()
    for t in ("tv_episodes", "tv_shows"):
        db.execute(f"DELETE FROM {t} WHERE rel_path LIKE 'tv/%' OR rel_path LIKE 'users/%' OR rel_path LIKE 'loose/%'")
    db.commit()
    for d in ("tv", "loose", os.path.join("users", "ann")):
        shutil.rmtree(os.path.join(app.MEDIA_DIR, d), ignore_errors=True)


def _index(host, app, rel):
    ap = _write(app, rel, b"\x00" * 64)
    assert host.emit("file.index", rel_path=rel, abs_path=ap, force=True) == [], \
        "a video is indexed by the core too: the tv module never claims it"
    return ap


SHOW = "tv/The Show (2001)"


def test_tv_section_is_registered(host):
    sec = host.library_sections()["tv"]
    assert sec["label"] == "TV" and sec["kinds"] == {"video", "image", "book"} and sec["per_user"]


def test_video_is_an_episode_only_inside_the_section(app, host, tree):
    inside = f"{SHOW}/Season 01/The Show - s01e01 - Pilot.mkv"
    personal = "users/ann/tv/Other (1999)/Specials/Other - s00e02.mkv"
    outside = "loose/The Show (2001)/Season 01/The Show - s01e01 - Pilot.mkv"
    for rel in (inside, personal, outside):
        _index(host, app, rel)
    e = _ep(app, inside)
    assert e and (e["season"], e["episode"], e["title"], e["show_rel"]) == (1, 1, "Pilot", SHOW)
    s = _show(app, SHOW)
    assert s and s["title"] == "The Show" and s["year"] == 2001 and s["base"] == "tv"
    p = _ep(app, personal)
    assert p and p["season"] == 0 and p["episode"] == 2 and p["show_rel"] == "users/ann/tv/Other (1999)"
    assert _show(app, "users/ann/tv/Other (1999)")["base"] == "users/ann/tv"
    assert _ep(app, outside) is None
    # a video the layout does not place is not an episode
    stray = f"{SHOW}/Season 01/random.mkv"
    _index(host, app, stray)
    assert _ep(app, stray) is None


def test_nfo_and_sidecars_are_attached(app, host, tree):
    _write(app, f"{SHOW}/poster.jpg", b"\xff\xd8")
    _write(app, f"{SHOW}/fanart.jxl", b"")
    _write(app, f"{SHOW}/season01-poster.png", b"")
    _write(app, f"{SHOW}/tvshow.nfo", b"<tvshow><title>The Show!</title><year>2002</year><plot>About.</plot>"
                                      b"<genre>Drama</genre></tvshow>")
    sdir = f"{SHOW}/Season 01"
    stem = "The Show - s01e01 - Pilot"
    _write(app, f"{sdir}/{stem}.nfo", b"<episodedetails><title>The Pilot</title><plot>Starts.</plot>"
                                      b"<aired>2001-05-01</aired></episodedetails>")
    _write(app, f"{sdir}/{stem}.en.srt", b"1\n00:00:01,000 --> 00:00:02,000\nHi\n")
    _write(app, f"{sdir}/{stem}-thumb.jpg", b"")
    rel = f"{sdir}/{stem}.mkv"
    _index(host, app, rel)
    s = _show(app, SHOW)
    assert (s["title"], s["year"], s["plot"], movie_import.loads(s["genres"])) == ("The Show!", 2002, "About.", ["Drama"])
    assert s["poster_rel"] == f"{SHOW}/poster.jpg" and s["fanart_rel"] == f"{SHOW}/fanart.jxl"
    assert s["nfo_rel"] == f"{SHOW}/tvshow.nfo"
    e = _ep(app, rel)
    assert (e["title"], e["plot"], e["aired"]) == ("The Pilot", "Starts.", "2001-05-01")
    assert e["season_poster_rel"] == f"{SHOW}/season01-poster.png"
    assert e["thumb_rel"] == f"{sdir}/{stem}-thumb.jpg" and e["nfo_rel"] == f"{sdir}/{stem}.nfo"
    assert movie_import.loads(e["subtitles"]) == [{"rel": f"{sdir}/{stem}.en.srt", "lang": "en", "forced": False, "sdh": False}]
    # a poster arriving later refreshes the show (the upload.stored hook)
    os.remove(os.path.join(app.MEDIA_DIR, f"{SHOW}/tvshow.nfo"))
    host.emit("upload.stored", rel_path=f"{SHOW}/poster.jpg", filename="poster.jpg")
    assert _show(app, SHOW)["title"] == "The Show" and _show(app, SHOW)["nfo_rel"] is None


def test_browse_routes(app, host, tree, client):
    eps = [f"{SHOW}/Season 01/The Show - s01e01 - Pilot.mkv", f"{SHOW}/Season 01/The Show - s01e02-e03 - Two.mkv",
           f"{SHOW}/Season 02/The Show - 2x01.mkv", f"{SHOW}/Specials/The Show - s00e01 - Xmas.mkv",
           f"{SHOW}/Season 01/Trailers/promo.mkv", "tv/Alpha (1990)/Season 1/Alpha - S01E01.mkv"]
    for rel in eps:
        _index(host, app, rel)
    j = client.get("/api/tv/shows").get_json()
    assert j["success"] and [s["title"] for s in j["shows"]] == ["Alpha", "The Show"]
    the = j["shows"][1]
    assert the["rel_path"] == SHOW and the["seasons"] == 3 and the["episodes"] == 4
    assert [s["title"] for s in client.get("/api/tv/shows?sort=year&dir=desc").get_json()["shows"]] == ["The Show", "Alpha"]
    assert [s["title"] for s in client.get("/api/tv/shows?q=alp").get_json()["shows"]] == ["Alpha"]
    se = client.get("/api/tv/seasons", query_string={"show": SHOW}).get_json()
    assert se["success"] and [(s["season"], s["episodes"], s["label"]) for s in se["seasons"]] == [
        (0, 1, "Specials"), (1, 2, "Season 1"), (2, 1, "Season 2")]
    ep = client.get("/api/tv/episodes", query_string={"show": SHOW, "season": 1}).get_json()
    assert ep["success"] and [(e["episode"], e["episode_end"], e["title"]) for e in ep["episodes"]] == [
        (1, None, "Pilot"), (2, 3, "Two")]
    ep = client.get("/api/tv/episodes", query_string={"show": SHOW, "season": 1, "extras": 1}).get_json()
    assert [e["extra"] for e in ep["episodes"]] == [None, None, "trailer"]
    assert len(client.get("/api/tv/episodes", query_string={"show": SHOW}).get_json()["episodes"]) == 4
    one = client.get("/api/tv/episode", query_string={"rel_path": eps[1]}).get_json()
    assert one["success"] and one["episode"]["episode_end"] == 3 and one["episode"]["show"]["title"] == "The Show"
    assert client.get("/api/tv/episode", query_string={"rel_path": "nope.mkv"}).status_code == 404
    assert client.get("/api/tv/seasons", query_string={"show": "tv/Nope (2000)"}).status_code == 404
    st = client.get("/api/tv/status").get_json()
    assert st["success"] and st["shows"] >= 2 and st["episodes"] >= 5 and st["view"]["rows"]
    assert client.post("/api/tv/reindex", json={}).get_json()["success"]


def test_rename_delete_and_sync(app, host, tree):
    inside = f"{SHOW}/Season 01/The Show - s01e01 - Pilot.mkv"
    outside = "loose/The Show - s01e01 - Pilot.mkv"
    ap = _index(host, app, inside)
    assert _show(app, SHOW)
    os.makedirs(os.path.join(app.MEDIA_DIR, "loose"), exist_ok=True)
    os.replace(ap, os.path.join(app.MEDIA_DIR, outside))
    host.emit("file.renamed", old_rel=inside, new_rel=outside)
    assert _ep(app, inside) is None and _ep(app, outside) is None
    assert _show(app, SHOW) is None, "a show without episodes has no row"
    os.makedirs(os.path.dirname(ap), exist_ok=True)
    os.replace(os.path.join(app.MEDIA_DIR, outside), ap)
    host.emit("file.renamed", old_rel=outside, new_rel=inside)
    assert _ep(app, inside) and _show(app, SHOW)
    host.emit("file.deleted", rel_path=inside)
    assert _ep(app, inside) is None and _show(app, SHOW) is None
    # a full pull walks the folders: the file is found again, a stale row goes
    host.update_file("tv/Gone (2000)/Season 01/Gone - s01e01.mkv", table="tv_episodes", dont_write=True,
                     set={"show_rel": "tv/Gone (2000)", "season": 1, "episode": 1})
    host.emit("library.sync", direction="pull", rel_paths=None)
    assert _ep(app, inside) and _show(app, SHOW)
    assert _ep(app, "tv/Gone (2000)/Season 01/Gone - s01e01.mkv") is None
    assert host.get_service("tv")["state"]["status"] == "idle"


def test_enricher_and_subtitles(app, host, tree, client):
    sdir = f"{SHOW}/Season 01"
    rel = f"{sdir}/The Show - s01e04 - Four.mkv"
    _write(app, f"{sdir}/The Show - s01e04 - Four.de.srt", b"1\n00:00:01,000 --> 00:00:02,000\nHallo\n")
    _index(host, app, rel)
    rows = [{"filename": rel}, {"filename": "other.jxl"}]
    host.enrich_file_rows(host.db(), rows)
    assert (rows[0]["show"], rows[0]["season"], rows[0]["episode"], rows[0]["episode_title"]) == ("The Show", 1, 4, "Four")
    assert "show" not in rows[1]
    r = client.get(f"/api/tv/subtitle/{sdir}/The Show - s01e04 - Four.de.srt")
    assert r.status_code == 200 and r.mimetype == "text/vtt" and b"Hallo" in r.data
    assert client.get(f"/api/tv/subtitle/{rel}").status_code == 404


def test_upload_of_a_non_video_kind_is_accepted(app, client, tree):
    """! @brief Nothing is refused inside the section: an mp3 in a season folder is
    stored as audio and is no episode."""
    r = client.post("/api/upload", data={"file": (io.BytesIO(b"\x00" * 16), "song.mp3"),
                                         "mode": "sync", "folder": f"{SHOW}/Season 01"},
                    content_type="multipart/form-data")
    assert r.status_code == 200, r.get_data(as_text=True)
    j = r.get_json()
    assert j["success"] and j["filename"].startswith(f"{SHOW}/Season 01/song.")
    assert os.path.isfile(os.path.join(app.MEDIA_DIR, j["filename"]))
    if app.mt.is_audio("x.mp3"):  # audio is a kind only with the music module (mutagen) on
        assert app.mt.kind(j["filename"]) == "audio"
    assert _ep(app, j["filename"]) is None