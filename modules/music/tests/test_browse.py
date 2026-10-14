"""! @file
@brief Music browser: sorting and filtering are the same across albums / artists /
genres / songs, blanks sort last, and an album lists in track order.

Rows are inserted straight into the music table (no audio decoded)."""
import time

import pytest


_ROWS = [
    # rel_path,               title,     artist,  albumartist, album,  track, disc, year,  genre,  dur,  created
    ("lib/a/1.mp3",          "Zulu",    "Alpha", "",          "First", 2,     1,    "1999", "Rock", 200., 10.),
    ("lib/a/2.mp3",          "Yankee",  "Alpha", "",          "First", 1,     1,    "1999", "Rock", 100., 11.),
    ("lib/a/3.mp3",          "Xray",    "Guest", "Alpha",     "First", 3,     1,    "1999", "Rock", 300., 12.),
    ("lib/b/1.mp3",          "Whiskey", "Bravo", "",          "Second", 1,    1,    "2005", "Jazz", 150., 30.),
    ("lib/b/2.mp3",          "Victor",  "Bravo", "",          "Second", 2,    1,    "",     "",     250., 31.),
    ("lib/c/1.mp3",          "Uniform", "",      "",          "",      None,  None, "",     "",     50.,  5.),
]


@pytest.fixture
def lib(app):
    db = app.module_host.db()
    for r in _ROWS:
        db.execute("INSERT OR REPLACE INTO music(rel_path,title,artist,albumartist,album,track,disc,year,genre,"
                   "duration,created,tags,mtime) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (*r, "[]", time.time()))
    db.commit()
    yield
    db.execute("DELETE FROM music WHERE rel_path LIKE 'lib/%'"); db.commit()


def _titles(client, **qs):
    j = client.get("/api/music/songs", query_string=qs).get_json()
    assert j["success"], j
    return [s["title"] for s in j["songs"] if s["rel_path"].startswith("lib/")]


def test_album_plays_in_track_order(client, lib):
    # the album artist ("Alpha") groups the guest track with the rest
    assert _titles(client, album="First", artist="Alpha") == ["Yankee", "Zulu", "Xray"]


def test_song_sorts(client, lib):
    assert _titles(client, sort="title") == ["Uniform", "Victor", "Whiskey", "Xray", "Yankee", "Zulu"]
    assert _titles(client, sort="title", dir="desc")[0] == "Zulu"
    assert _titles(client, sort="duration")[0] == "Uniform"
    assert _titles(client, sort="added", dir="desc")[0] == "Victor"
    # a blank year sorts last whichever way
    asc, desc = _titles(client, sort="year"), _titles(client, sort="year", dir="desc")
    assert asc[0] in ("Zulu", "Yankee", "Xray") and desc[0] in ("Whiskey",)
    assert set(asc[-2:]) == {"Victor", "Uniform"} and set(desc[-2:]) == {"Victor", "Uniform"}


def test_filters_narrow_every_view(client, lib):
    assert set(_titles(client, genre="Jazz")) == {"Whiskey"}
    assert set(_titles(client, genre="(none)")) == {"Victor", "Uniform"}
    assert set(_titles(client, q="bravo")) == {"Whiskey", "Victor"}
    assert set(_titles(client, q="bravo 2005")) == {"Whiskey"}      # every word must match
    albums = client.get("/api/music/albums", query_string={"q": "bravo"}).get_json()["albums"]
    assert [a["album"] for a in albums if a["artist"] == "Bravo"] == ["Second"]
    assert not [a for a in albums if a["artist"] == "Alpha"]
    artists = client.get("/api/music/artists", query_string={"genre": "Rock"}).get_json()["artists"]
    assert [a["name"] for a in artists] == ["Alpha"]
    genres = {g["name"]: g["tracks"] for g in client.get("/api/music/genres").get_json()["genres"]}
    assert genres.get("Rock") == 3 and genres.get("Jazz") == 1


def test_group_sorts(client, lib):
    names = [a["name"] for a in client.get("/api/music/artists", query_string={"sort": "tracks", "dir": "desc"})
             .get_json()["artists"] if a["name"] in ("Alpha", "Bravo", "(unknown)")]
    assert names[0] == "Alpha"
    albums = [a["album"] for a in client.get("/api/music/albums", query_string={"sort": "year"})
              .get_json()["albums"] if a["artist"] in ("Alpha", "Bravo", "(unknown)")]
    assert albums == ["First", "Second", "(unknown)"]


def test_paging(client, lib):
    j = client.get("/api/music/songs", query_string={"q": "lib/", "per": 2, "page": 1, "sort": "title"}).get_json()
    assert j["total"] == 6 and [s["title"] for s in j["songs"]] == ["Whiskey", "Xray"]