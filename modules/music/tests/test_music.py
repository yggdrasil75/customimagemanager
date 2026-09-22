"""Music module: an uploaded mp3 is indexed from its tags, streamable, editable."""
import time
import pytest


def _song(client, rel, tries=30):
    for _ in range(tries):
        for s in client.get("/api/music/songs").get_json().get("songs", []):
            if s["rel_path"] == rel:
                return s
        client.post("/api/music/reindex", json={})
        time.sleep(0.3)
    return None


@pytest.fixture
def mp3(upload):
    return upload.media("song.mp3")


def test_indexed_from_id3(client, mp3):
    s = _song(client, mp3)
    assert s, "mp3 never showed up in /api/music/songs"
    assert s["title"] and s["artist"], "title/artist should come from the ID3 tags"
    assert s["duration"] and s["duration"] > 0
    assert client.get("/api/music/artists").get_json()["artists"], "artist list empty after indexing"


def test_stream(client, mp3):
    r = client.get(f"/api/music/stream/{mp3}")
    assert r.status_code in (200, 206) and len(r.data) > 1000
    assert client.get("/api/music/stream/nope.mp3").status_code == 404


def test_edit_tags_writes_file_and_index(client, mp3):
    assert _song(client, mp3)
    j = client.post("/api/music/meta", json={"rel_path": mp3, "title": "Renamed Tone",
                                              "tags": ["test"]}).get_json()
    assert j["success"] and j["file_written"]
    s = _song(client, mp3)
    assert s["title"] == "Renamed Tone" and s["tags"] == ["test"]
    assert client.post("/api/music/meta", json={"rel_path": "nope.mp3"}).status_code == 404


def test_status(client):
    j = client.get("/api/music/status").get_json()
    assert j["success"] and "tracks" in j
