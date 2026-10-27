"""! @file
@brief Music: one song across albums (versions) and music videos.

A band's hit on 15 albums is 15 rows with near-identical embeddings; radio,
shuffle-by and similar must treat them as one song so they never play back to
back. A video in the music section that is the same song plays in the player in
place of the visualizer. No audio is decoded (a fake embed.audio provider), so
this runs on any machine.
"""
import os
import shutil
import time

import numpy as np
import pytest

from modules.music import music_lib as ml


# -- song_key ---------------------------------------------------------------------
@pytest.mark.parametrize("a,b", [
    (("Saturday in the Park", "Chicago"), ("Saturday In The Park (2002 Remaster)", "Chicago")),
    (("Saturday in the Park", "Chicago"), ("Saturday in the Park - Live at Carnegie Hall", "Chicago")),
    (("25 or 6 to 4", "Chicago"), ("05 - 25 or 6 to 4 [Single Version]", "The Chicago")),
    (("If You Leave Me Now", "Chicago"), ("If You Leave Me Now (feat. Someone)", "Chicago feat. Someone")),
    (("Halo", "Beyoncé"), ("Halo (Official Music Video) [bnVUHWCynig]", "Beyonce")),
    (("Rock & Roll", "X"), ("Rock and Roll (Mono)", "X")),
])
def test_versions_share_a_key(a, b):
    assert ml.song_key(*a) == ml.song_key(*b) != ""


@pytest.mark.parametrize("a,b", [
    (("Make Me Smile (Part 1)", "Chicago"), ("Make Me Smile (Part 2)", "Chicago")),
    (("Saturday in the Park", "Chicago"), ("Saturday in the Park", "Someone Else")),
    (("25 or 6 to 4", "Chicago"), ("6 to 4", "Chicago")),
])
def test_different_songs_differ(a, b):
    assert ml.song_key(*a) != ml.song_key(*b)


def test_track_artist_beats_album_artist():
    assert ml.song_key("Hard Habit to Break", "Chicago", "Various Artists") == \
           ml.song_key("Hard Habit to Break", "", "Chicago")


def test_spread_versions_keeps_versions_apart():
    out = ml.spread_versions(list("aabbbcdef"), lambda x: x)
    assert sorted(out) == sorted("aabbbcdef")
    assert all(x != y for x, y in zip(out, out[1:])), out


# -- the library ------------------------------------------------------------------
def _unit(v):
    v = np.asarray(v, np.float32)
    return v / np.linalg.norm(v)


_RNG = np.random.default_rng(11)
_SONGS = {s: _unit(_RNG.normal(size=32)) for s in
          ("Saturday in the Park", "25 or 6 to 4", "Beginnings", "Colour My World",
           "Make Me Smile", "Old Days", "Just You N Me", "Feelin Stronger Every Day")}
_ALBUMS = ["Chicago V", "Greatest Hits", "Live in Japan", "The Very Best", "Chicago XXX"]
_SUFFIX = {"Live in Japan": " (Live)", "Chicago XXX": " - 2002 Remaster"}


def _vec(rel):
    song = rel.split("/")[-1].rsplit(".", 1)[0].split(" - ", 1)[-1]
    r = np.random.default_rng(abs(hash(rel)) % (1 << 31))
    return _unit(_SONGS[song] + 0.02 * r.normal(size=32))     # versions: almost the same sound


@pytest.fixture
def chicago(app, fake_model):
    fn = lambda path, *a, **k: _vec(path.replace(app.module_host.media_dir, "").strip("/\\"))
    fn.space = "test-audio"
    fake_model("embed.audio", fn)
    db = app.module_host.db()
    rows = []
    for al in _ALBUMS:
        for i, song in enumerate(_SONGS):
            rel = f"music/Chicago/{al}/{i + 1:02d} - {song}.flac"
            db.execute("INSERT OR REPLACE INTO music(rel_path,title,artist,albumartist,album,duration,tags,"
                       "emb,emb_sig,created) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (rel, song + _SUFFIX.get(al, ""), "Chicago",
                        "Various Artists" if al == "The Very Best" else "Chicago", al, 240.0, "[]",
                        ml._pack_emb(_vec(rel)), "test-audio", time.time()))
            rows.append(rel)
    db.commit()
    yield rows
    db.execute("DELETE FROM music"); db.execute("DELETE FROM music_plays")
    db.execute("DELETE FROM music_radio"); db.commit()
    shutil.rmtree(os.path.join(app.MEDIA_DIR, "music"), ignore_errors=True)


def _key(s):
    return ml.song_key(s["title"], s["artist"], s["albumartist"])


def test_songs_carry_their_key(client, chicago):
    d = client.get("/api/music/songs", query_string={"q": "Saturday", "per": 50}).get_json()
    assert len(d["songs"]) == len(_ALBUMS)
    assert len({s["song_key"] for s in d["songs"]}) == 1


def test_radio_plays_each_song_once(client, chicago):
    d = client.post("/api/music/radio/next", json={"seed": 2}).get_json()
    assert d["success"], d
    keys = [_key(s) for s in d["songs"]]
    assert len(keys) == len(set(keys)) == len(_SONGS)
    assert d["info"]["versions_skipped"] == len(chicago) - len(_SONGS)


def test_radio_rotates_versions_across_rounds(client, chicago):
    picked = set()
    for seed in range(12):
        d = client.post("/api/music/radio/next", json={"seed": seed}).get_json()
        picked |= {s["rel_path"] for s in d["songs"] if s["title"].startswith("Saturday")}
    assert len(picked) > 1, "every round played the same version"


def test_radio_defers_other_versions_of_a_recent_song(client, chicago, app):
    app.module_host.config["music_radio_min_gap_hours"] = 0.25      # 15 min = ~4 tracks
    client.post("/api/music/radio/played", json={"rel_path": chicago[0]})   # Chicago V / Saturday
    d = client.post("/api/music/radio/next", json={"seed": 9}).get_json()
    order = [_key(s) for s in d["songs"]]
    i = order.index(ml.song_key("Saturday in the Park", "Chicago"))
    assert i * 240.0 >= 0.25 * 3600 - 240.0, i


def test_similar_skips_versions(client, chicago):
    d = client.post("/api/music/similar", json={"rel_path": chicago[0], "top_k": 5}).get_json()
    assert d["success"], d
    keys = [_key(s) for s in d["songs"]]
    assert ml.song_key("Saturday in the Park", "Chicago") not in keys
    assert len(keys) == len(set(keys)) == 5
    raw = client.post("/api/music/similar", json={"rel_path": chicago[0], "top_k": 4,
                                                   "distinct": False}).get_json()["songs"]
    assert all(s["title"].startswith("Saturday") for s in raw), "without distinct: the versions"


def test_shuffle_by_artist_is_one_per_song(client, chicago):
    d = client.post("/api/music/shuffle", json={"seed_type": "artist", "seed": "Chicago"}).get_json()
    assert d["success"], d
    keys = [_key(s) for s in d["songs"]]
    assert len(keys) == len(set(keys)) == len(_SONGS)


# -- music videos -------------------------------------------------------------------
def _touch(app, host, rel):
    ap = os.path.join(app.MEDIA_DIR, rel)
    os.makedirs(os.path.dirname(ap), exist_ok=True)
    with open(ap, "wb") as f:
        f.write(b"\x00" * 64)
    host.emit("file.index", rel_path=rel, abs_path=ap, force=True)
    return ap


def _song(client, rel):
    return client.get("/api/music/track", query_string={"rel_path": rel}).get_json()["song"]


def test_a_video_beside_a_track_is_its_video_only(client, chicago, app, host):
    track = "music/Chicago/Chicago V/01 - Saturday in the Park.flac"
    vid = "music/Chicago/Chicago V/01 - Saturday in the Park.mp4"
    _touch(app, host, vid)
    assert _song(client, track)["video"] == vid
    # the other albums' versions of the song don't get it
    for al in _ALBUMS[1:]:
        other = next(r for r in chicago if r.startswith(f"music/Chicago/{al}/") and "Saturday" in r)
        assert _song(client, other)["video"] is None
    # the video's own row is the same song as its track
    assert _song(client, vid)["song_key"] == _song(client, track)["song_key"]


def test_a_standalone_video_is_a_track_of_its_own(client, chicago, app, host):
    vid = "music/Chicago/Singles/Old Days.mp4"
    _touch(app, host, vid)
    assert _song(client, vid)["video"] == vid
    for r in chicago:
        assert _song(client, r)["video"] is None


def test_a_track_video_and_its_track_play_once_per_round(client, chicago, app, host):
    vid = "music/Chicago/Chicago V/06 - Old Days.mp4"
    _touch(app, host, vid)
    db = app.module_host.db()
    db.execute("UPDATE music SET emb=?, emb_sig=? WHERE rel_path=?",
               (ml._pack_emb(_SONGS["Old Days"]), "test-audio", vid))
    db.commit()
    d = client.post("/api/music/radio/next", json={"seed": 4}).get_json()
    keys = [x["song_key"] for x in d["songs"]]
    assert len(keys) == len(set(keys)) == len(_SONGS)


def test_deleted_video_drops_out(client, chicago, app, host):
    track = "music/Chicago/Chicago V/06 - Old Days.flac"
    vid = "music/Chicago/Chicago V/06 - Old Days.mp4"
    ap = _touch(app, host, vid)
    assert _song(client, track)["video"] == vid
    os.remove(ap)
    host.emit("file.deleted", rel_path=vid)
    assert _song(client, track)["video"] is None


def test_artist_folder_tracks_and_concerts(client, chicago, app, host):
    """! @brief Compilation-style tracks straight in the artist folder pair with a video
    of the same name there; a full concert video there is a track of its own."""
    db = app.module_host.db()
    loose = "music/Chicago/Hard Habit to Break.flac"
    db.execute("INSERT OR REPLACE INTO music(rel_path,title,artist,album,duration,tags,created) "
               "VALUES(?,?,?,?,?,?,?)", (loose, "Hard Habit to Break", "Chicago", "", 240.0, "[]", time.time()))
    db.commit()
    vid = "music/Chicago/Hard Habit to Break.mp4"
    concert = "music/Chicago/Live at Tanglewood 1970.mp4"
    _touch(app, host, vid)
    _touch(app, host, concert)
    assert _song(client, loose)["video"] == vid
    assert _song(client, concert)["video"] == concert
    assert _song(client, concert)["song_key"] != _song(client, loose)["song_key"]
    assert all(_song(client, r)["video"] is None for r in chicago)