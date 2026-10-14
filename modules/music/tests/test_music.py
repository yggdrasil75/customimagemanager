"""! @file
@brief Music module: an uploaded mp3 is indexed from its tags, streamable, editable."""
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
    """! @brief The fixture must carry ID3 title+artist; without them there is nothing
    for the indexer to read and the test would be about the file, not the code."""
    from cimtest import fixture as fixture_path
    path = fixture_path("song.mp3")
    try:
        import mutagen
        tags = mutagen.File(path, easy=True) or {}
        if not (tags.get("title") and tags.get("artist")):
            pytest.skip(f"{path} has no ID3 title/artist - tag it (see tests/fixtures/README.md)")
    except ImportError:
        pass
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


def test_track_detail_and_cover(client, mp3):
    s = _song(client, mp3)
    assert s
    j = client.get(f"/api/music/track?rel_path={mp3}").get_json()
    assert j["success"] and j["song"]["rel_path"] == mp3
    assert client.get("/api/music/track?rel_path=nope.mp3").status_code == 404
    r = client.get(f"/api/music/cover/{mp3}")
    # the fixture may or may not carry art; either way the answer is an image or a clean 404
    assert r.status_code in (200, 404)
    if r.status_code == 200:
        assert r.mimetype.startswith("image/") and len(r.data) > 0
        assert _song(client, mp3)["has_cover"] is True


def test_track_in_gallery(client, mp3):
    assert _song(client, mp3)
    files = client.get("/api/list?q=&folder=&page=0&recursive=1").get_json().get("files", [])
    hit = [f for f in files if f.get("kind") == "audio" and f["rel_path"] == mp3]
    assert hit, "track should show in the gallery as an audio tile"
    # an image-only token leaves tracks out
    files = client.get("/api/list?q=is:tagged&folder=&page=0").get_json().get("files", [])
    assert not [f for f in files if f.get("kind") == "audio"]