"""! @file
@brief Music: the audio embedding pick, similar / sem: search and radio rounds.

No audio is decoded: a fake `embed.audio` provider returns a fixed vector per
path (with a .embed_text tower), and tracks are inserted straight into the
music table, so this runs on any machine.
"""
import json
import time

import numpy as np
import pytest

import features
from cimtest import signed_in_as


def _unit(v):
    v = np.asarray(v, np.float32)
    return v / np.linalg.norm(v)


# 8 "genres" as unit centroids; each track is a jittered centroid so a route
# through the space should keep neighbouring tracks in the same genre.
_RNG = np.random.default_rng(7)
_GENRES = {g: _unit(_RNG.normal(size=32)) for g in
           ("rock", "blues", "jazz", "classical", "pop", "metal", "folk", "xmas")}


def _vec(rel):
    g = rel.split("/")[0]
    r = np.random.default_rng(abs(hash(rel)) % (1 << 31))
    return _unit(_GENRES[g] + 0.15 * r.normal(size=32))


@pytest.fixture
def library(app, fake_model):
    from modules.music import music_lib as ml
    fn = lambda path, *a, **k: _vec(path.replace(app.module_host.media_dir, "").strip("/\\"))
    fn.embed_text = lambda text: _GENRES["xmas"] if "christmas" in text.lower() else _GENRES["jazz"]
    fn.space = "test-audio"
    fake_model("embed.audio", fn)
    db = app.module_host.db()
    paths = []
    for g in _GENRES:
        for i in range(6):
            rel = f"{g}/track{i}.mp3"
            title = "Jingle Bells" if (g == "xmas" and i == 0) else f"{g} {i}"
            db.execute("INSERT OR REPLACE INTO music(rel_path,title,artist,album,genre,duration,tags,"
                       "emb,emb_sig,created) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (rel, title, g + " band", g + " album", "Christmas" if g == "xmas" else g,
                        240.0, "[]", ml._pack_emb(_vec(rel)), "test-audio", time.time()))
            paths.append(rel)
    db.commit()
    yield paths
    db.execute("DELETE FROM music"); db.execute("DELETE FROM music_plays")
    db.execute("DELETE FROM music_radio"); db.commit()


def test_status_reports_pick(client, library):
    d = client.get("/api/music/status").get_json()
    assert d["space"] == "test-audio" and d["text_search"] is True
    assert d["embedded"] == len(library)


def test_similar_stays_in_genre(client, library):
    d = client.post("/api/music/similar", json={"rel_path": "jazz/track0.mp3", "top_k": 5}).get_json()
    assert d["success"], d
    assert all(s["rel_path"].startswith("jazz/") for s in d["songs"])
    assert "jazz/track0.mp3" not in [s["rel_path"] for s in d["songs"]]


def test_semantic_search_through_text_tower(client, library):
    d = client.get("/api/music/songs?q=sem:christmas").get_json()
    assert d["success"] and d["mode"] == "semantic", d
    assert d["songs"][0]["rel_path"].startswith("xmas/")


def test_radio_round_is_a_smooth_walk_without_seasonal(client, library):
    d = client.post("/api/music/radio/next", json={"seed": 1}).get_json()
    assert d["success"], d
    order = [s["rel_path"] for s in d["songs"]]
    assert not any(p.startswith("xmas/") for p in order), "genre 'Christmas' should be left out"
    assert set(order) == {p for p in library if not p.startswith("xmas/")}
    # a walk: most consecutive pairs share a genre (8 genres of 6 -> >= 5 same-genre
    # steps per genre if the route is coherent; random order would give ~1/7)
    same = sum(a.split("/")[0] == b.split("/")[0] for a, b in zip(order, order[1:]))
    assert same >= 0.7 * (len(order) - 1), same
    assert d["info"]["round"] == 1 and d["info"]["seasonal_skipped"] == 6


def test_radio_next_round_continues_and_repeats_loved(client, library, app):
    db = app.module_host.db()
    db.execute("CREATE TABLE IF NOT EXISTS ratings(rel_path TEXT PRIMARY KEY, user_stars INTEGER, "
               "iqa_stars INTEGER, iqa_raw REAL, iqa_model TEXT)")
    for i in range(6):                                   # blues sits out, rock is loved
        db.execute("INSERT OR REPLACE INTO ratings(rel_path,user_stars) VALUES(?,?)", (f"blues/track{i}.mp3", 0))
        db.execute("INSERT OR REPLACE INTO ratings(rel_path,user_stars) VALUES(?,?)", (f"rock/track{i}.mp3", 5))
    db.commit()
    app.module_host.config["music_radio_min_gap_hours"] = 0.5     # 30 min of 4-min tracks → gap fits
    r1 = client.post("/api/music/radio/next", json={"seed": 3}).get_json()
    o1 = [s["rel_path"] for s in r1["songs"]]
    r2 = client.post("/api/music/radio/next", json={"seed": 4}).get_json()
    o2 = [s["rel_path"] for s in r2["songs"]]
    assert r2["info"]["round"] == r1["info"]["round"] + 1
    # round 2 starts near where round 1 ended
    assert o2[0].split("/")[0] == o1[-1].split("/")[0]
    # loved tracks may appear twice, never back to back, at least the gap apart
    for o in (o1, o2):
        for p in set(o):
            idx = [i for i, q in enumerate(o) if q == p]
            if len(idx) > 1:
                assert p.startswith("rock/")
                assert (idx[1] - idx[0]) * 240.0 >= 0.5 * 3600 - 240.0
    assert any(len([q for q in o1 if q == p]) == 2 for p in set(o1)) or \
           any(len([q for q in o2 if q == p]) == 2 for p in set(o2))
    # 0-star tracks mostly sit rounds out
    assert sum(p.startswith("blues/") for p in o1 + o2) < 12


def test_radio_played_log_defers_recent(client, library, app):
    app.module_host.config["music_radio_min_gap_hours"] = 4
    for i in range(6):
        client.post("/api/music/radio/played", json={"rel_path": f"jazz/track{i}.mp3"})
    d = client.post("/api/music/radio/next", json={"seed": 5}).get_json()
    order = [s["rel_path"] for s in d["songs"]]
    assert d["success"]
    # every just-played jazz track is pushed at least 4 h of playback in (60 x 4-min tracks)
    first_jazz = min(i for i, p in enumerate(order) if p.startswith("jazz/"))
    assert first_jazz * 240.0 >= 4 * 3600 - 240.0 or len(order) * 240.0 < 4 * 3600


# -- another account's tracks --------------------------------------------------------------
@pytest.fixture
def listener(client):
    """! @brief A real non-admin account that may use the Music tab."""
    r = client.post("/api/auth/users/create", json={"username": "mus_carol", "password": "pw", "role": "custom"})
    assert r.status_code == 200, r.get_json()
    u = r.get_json()["user"]
    yield {**u, "is_admin": False,
           "features": features.effective_permissions("custom", {"tab.music": "write"})}
    client.post("/api/auth/users/delete", json={"id": u["id"]})


def test_another_accounts_tracks_stay_private(app, client, library, listener):
    """! @brief Tracks in users/<name>/ follow the same rule as photos: another account
    sees them in no list, count, search, shuffle, similar or radio, and can't edit them."""
    from modules.music import music_lib as ml
    db = app.module_host.db()
    mine = [f"users/mus_dave/jazz/secret{i}.mp3" for i in range(3)]
    for rel in mine:
        db.execute("INSERT OR REPLACE INTO music(rel_path,title,artist,album,genre,duration,tags,"
                   "emb,emb_sig,created) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (rel, "Secret", "Dave Private", "Dave Album", "jazz", 240.0, "[]",
                    ml._pack_emb(_vec("jazz/" + rel.rsplit("/", 1)[1])), "test-audio", time.time()))
    db.commit()
    total = client.get("/api/music/status").get_json()["tracks"]          # the admin counts them
    with signed_in_as(app, listener):
        st = client.get("/api/music/status").get_json()
        assert st["tracks"] == total - 3 and st["embedded"] == len(library)
        songs = client.get("/api/music/songs", query_string={"q": "Secret"}).get_json()["songs"]
        assert songs == []
        assert "Dave Private" not in {a["name"] for a in client.get("/api/music/artists").get_json()["artists"]}
        assert "Dave Album" not in {a["album"] for a in client.get("/api/music/albums").get_json()["albums"]}
        sem = client.get("/api/music/songs", query_string={"q": "sem:jazz"}).get_json()["songs"]
        assert sem and not any(s["rel_path"] in mine for s in sem)
        sim = client.post("/api/music/similar", json={"rel_path": "jazz/track0.mp3"}).get_json()["songs"]
        assert sim and not any(s["rel_path"] in mine for s in sim)
        assert client.post("/api/music/similar", json={"rel_path": mine[0]}).get_json()["success"] is False
        shuf = client.post("/api/music/shuffle", json={"seed": "jazz/track0.mp3"}).get_json()["songs"]
        assert shuf and not any(s["rel_path"] in mine for s in shuf)
        assert client.post("/api/music/shuffle", json={"seed": mine[0]}).get_json()["success"] is False
        radio = client.post("/api/music/radio/next", json={"seed": 1}).get_json()["songs"]
        assert radio and not any(s["rel_path"] in mine for s in radio)
        assert client.post("/api/music/radio/played", json={"rel_path": mine[0]}).status_code == 404
        assert client.post("/api/music/meta", json={"rel_path": mine[0], "title": "x"}).status_code == 404
    sim = client.post("/api/music/similar", json={"rel_path": "jazz/track0.mp3"}).get_json()["songs"]
    assert any(s["rel_path"] in mine for s in sim)                            # the admin sees all