"""! @file
@brief Immich import against a fake Immich: albums, tags, named face boxes,
favourites, archived (via the new 'visibility' filter; the server rejects the
old 'withArchived'), trash skipped, live-photo video, capture time zone,
checksum-verified downloads; a periodic re-run asks only for changes and adds
new album memberships to photos imported earlier."""
import base64, hashlib, json
import pytest
from cimtest import png_bytes, read_meta
from modules.fetch.tests.importtest import imports, ledger, run_source  # noqa: F401

import modules.immich_import.module as immich_mod

VIDEO = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64


class FakeImmich:
    def __init__(self):
        self.blobs = {"a1": png_bytes(seed=801), "a2": png_bytes(seed=802), "a3": png_bytes(seed=803),
                      "a4": png_bytes(seed=804), "v4": VIDEO}
        self.corrupt = set()
        self.album_assets = {"al1": ["a1"]}
        self.search_bodies = []
        faces = [{"boundingBoxX1": 100, "boundingBoxY1": 50, "boundingBoxX2": 300, "boundingBoxY2": 250,
                  "imageWidth": 1000, "imageHeight": 500}]

        def asset(aid, name, **kw):
            d = {"id": aid, "type": "IMAGE", "originalFileName": name, "originalPath": f"upload/library/ann/2024/{name}",
                 "checksum": base64.b64encode(hashlib.sha1(self.blobs[aid]).digest()).decode(),
                 "fileCreatedAt": "2024-07-20T07:23:45.000Z", "localDateTime": "2024-07-20T09:23:45.000Z",
                 "updatedAt": "2024-07-21T00:00:00.000Z", "isFavorite": False, "isTrashed": False,
                 "exifInfo": {"dateTimeOriginal": "2024-07-20T07:23:45.000Z", "latitude": None, "longitude": None,
                              "description": ""}, "people": []}
            d.update(kw)
            return d
        self.assets = {
            "a1": asset("a1", "beach.png", isFavorite=True, people=[{"name": "Ann", "faces": faces}],
                        exifInfo={"dateTimeOriginal": "2024-07-20T07:23:45.000Z", "latitude": 48.85,
                                  "longitude": 2.35, "description": "sunset"}),
            "a2": asset("a2", "archived.png", visibility="archive"),
            "a3": asset("a3", "trashed.png", isTrashed=True),
            "a4": asset("a4", "live.png", livePhotoVideoId="v4"),
        }
        self.video = {"id": "v4", "originalFileName": "live.mov",
                      "checksum": base64.b64encode(hashlib.sha1(VIDEO).digest()).decode()}

    # the requests.Session surface the client uses
    headers = {}

    def request(self, method, url, json=None, params=None, stream=False, timeout=None):
        path = url.split("/api", 1)[1]
        if path == "/users/me":
            return Resp({"id": "u1", "name": "Ann"})
        if path == "/assets/statistics":
            return Resp({"total": 4})
        if path == "/albums":
            return Resp([{"id": "al1", "albumName": "Trip"}])
        if path == "/albums/al1":
            return Resp({"albumName": "Trip", "assets": [{"id": a} for a in self.album_assets["al1"]]})
        if path == "/tags":
            return Resp([{"id": "t1", "value": "Beach/Sunny"}])
        if path == "/search/metadata":
            self.search_bodies.append(dict(json))
            if "withArchived" in json:
                return Resp({"message": ["property withArchived should not exist"]}, 400)
            if json.get("tagIds"):
                items = [self.assets["a1"]]
            elif json.get("visibility") == "archive":
                items = [a for a in self.assets.values() if a.get("visibility") == "archive"]
            else:
                items = [a for a in self.assets.values() if a.get("visibility") != "archive"]
            if json.get("updatedAfter"):
                items = [a for a in items if a["updatedAt"] > json["updatedAfter"]]
            return Resp({"assets": {"items": items if json["page"] == 1 else [], "nextPage": None}})
        if path == "/assets/v4":
            return Resp(self.video)
        if path.startswith("/assets/a") and path.count("/") == 2:
            return Resp(self.assets[path.split("/")[2]])
        if path.endswith("/original"):
            aid = path.split("/")[2]
            data = self.blobs[aid]
            if aid in self.corrupt:
                data = data[:-10]
            return Resp(raw=data)
        return Resp({"message": "not found"}, 404)


class Resp:
    def __init__(self, body=None, status=200, raw=None):
        self.status_code, self._body, self._raw = status, body, raw
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        return self._body

    def iter_content(self, n):
        yield self._raw


@pytest.fixture
def fake(monkeypatch):
    srv = FakeImmich()
    monkeypatch.setattr(immich_mod.requests, "Session", lambda: srv)
    return srv


def _save(client, **cfg):
    r = client.post("/api/import/immich/save", json={"config": {"url": "https://immich.test", **cfg},
                                                     "secrets": {"api_key": "SECRET-KEY-123"}})
    assert r.status_code == 200, r.get_json()
    return r.get_json()["id"]


def test_immich_import_and_periodic(client, app, imports, fake):
    sid = _save(client, folder="immich/{year}", every_h=12)
    src = app._db().execute("SELECT label, secrets FROM import_sources WHERE id=?", (sid,)).fetchone()
    assert src["label"] == "Ann @ https://immich.test"
    assert "SECRET-KEY-123" not in json.dumps(client.get("/api/import/immich/state").get_json())  # never sent back
    row = run_source(client, app, "immich", sid)
    assert row["status"] == "done", row

    a1 = ledger(app, "immich", "beach.png")
    assert a1["status"] == "done" and a1["rel_path"].startswith("immich/2024/")
    assert a1["d_original"] == "2024-07-20"
    m = read_meta(client, a1["rel_path"])
    assert "favorite" in m["tags"] and "Beach/Sunny" in m["tags"] and m["description"] == "sunset"
    face = [r for r in m["regions"] if r.get("region_name") == "Ann"]
    assert face and abs(face[0]["cx"] - 0.2) < 1e-3 and abs(face[0]["w"] - 0.2) < 1e-3 and abs(face[0]["h"] - 0.4) < 1e-3
    assert "Trip" in app._file_albums(a1["rel_path"])
    xmp = open(app.MEDIA_DIR + "/" + a1["rel_path"].rsplit(".", 1)[0] + ".xmp").read()
    assert "2024-07-20T09:23:45+02:00" in xmp                         # local time with the photo's offset
    assert "archived" in read_meta(client, ledger(app, "immich", "archived.png")["rel_path"])["tags"]
    assert ledger(app, "immich", "trashed.png")["status"] == "skipped"
    assert ledger(app, "immich", "live.mov")["status"] == "done"      # live-photo video beside its still

    # periodic re-run: only changes are requested; an old photo newly in an album gets it
    fake.album_assets["al1"] = ["a1", "a2"]
    fake.search_bodies.clear()
    row = run_source(client, app, "immich", sid)
    assert row["downloaded"] == 0
    assert all("updatedAfter" in b for b in fake.search_bodies if not b.get("tagIds"))
    assert "Trip" in app._file_albums(ledger(app, "immich", "archived.png")["rel_path"])


def test_checksum_mismatch_fails_and_retries(client, app, imports, fake):
    fake.corrupt.add("a1")
    sid = _save(client)
    run_source(client, app, "immich", sid)
    r = ledger(app, "immich", "beach.png")
    assert r["status"] == "failed" and "checksum" in r["error"]
    fake.corrupt.clear()
    run_source(client, app, "immich", sid)                          # next run retries the failed one
    assert ledger(app, "immich", "beach.png")["status"] == "done"


def test_bad_key_refused_on_save(client, imports, monkeypatch):
    class Deny(FakeImmich):
        def request(self, *a, **k):
            return Resp({"message": "Invalid API key"}, 401)
    monkeypatch.setattr(immich_mod.requests, "Session", lambda: Deny())
    r = client.post("/api/import/immich/save", json={"config": {"url": "https://immich.test"}, "secrets": {"api_key": "x"}})
    assert r.status_code == 400 and "rejected the API key" in r.get_json()["error"]
