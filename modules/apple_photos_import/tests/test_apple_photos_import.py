"""Apple import: the privacy.apple.com export (CSVs, albums, live photos,
Recently Deleted), and live iCloud against a fake account — 2FA sign-in,
import, and a purge that only removes what is safely in the library, older
than the keep window, and not a favourite; an expired session asks to sign in."""
import os, time, zipfile
from datetime import datetime, timedelta, timezone
import pytest
from cimtest import png_bytes, read_meta
from modules.fetch.tests.importtest import imports, ledger, run_source  # noqa: F401

import modules.apple_photos_import.module as apple_mod
from pyicloud import exceptions as E

MOV = b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 64


# ── Apple data export ────────────────────────────────────────────────────────
def test_apple_export(client, app, imports):
    p = "iCloud Photos/"
    with zipfile.ZipFile(str(imports / "iCloud Photos Part 1 of 1.zip"), "w") as z:
        z.writestr(p + "Photos/IMG_0001.png", png_bytes(seed=901))
        z.writestr(p + "Photos/IMG_0002.png", png_bytes(seed=902))
        z.writestr(p + "Photos/IMG_0003.png", png_bytes(seed=903))
        z.writestr(p + "Photos/IMG_0003.mov", MOV)
        z.writestr(p + "Photo Details.csv",
                   "imgName,fileChecksum,favorite,hidden,deleted,originalCreationDate,viewCount,importDate\n"
                   "IMG_0001.png,ck1,yes,no,no,\"Saturday June 5,2021 2:48 PM GMT\",1,x\n"
                   "IMG_0002.png,ck2,no,no,yes,\"Saturday June 5,2021 2:49 PM GMT\",1,x\n"
                   "IMG_0003.png,ck3,no,yes,no,\"Sunday June 6,2021 9:00 AM GMT\",1,x\n")
        z.writestr(p + "Albums/Summer.csv", "Images\nIMG_0001.png\nIMG_0003.png\n")
    r = client.post("/api/import/apple_export/save", json={"config": {"path": "iCloud Photos Part 1 of 1.zip"}})
    sid = r.get_json()["id"]
    assert run_source(client, app, "apple_export", sid)["status"] == "done"
    one = ledger(app, "apple_export", "IMG_0001.png")
    assert one["status"] == "done" and one["d_original"] == "2021-06-05" and one["rel_path"].startswith("apple-photos/2021/")
    assert "favorite" in read_meta(client, one["rel_path"])["tags"]
    assert "Summer" in app._file_albums(one["rel_path"])
    assert ledger(app, "apple_export", "IMG_0002.png")["status"] == "skipped"          # Recently Deleted
    three = ledger(app, "apple_export", "IMG_0003.png")
    assert "hidden" in read_meta(client, three["rel_path"])["tags"]
    mov = ledger(app, "apple_export", "IMG_0003.mov")
    assert mov["status"] == "done" and "Summer" in app._file_albums(mov["rel_path"])   # live video follows its still


# ── live iCloud, faked ───────────────────────────────────────────────────────
OLD = datetime.now(timezone.utc) - timedelta(days=400)
NEW = datetime.now(timezone.utc) - timedelta(days=2)


class Asset:
    def __init__(self, id, filename, date, blob, favorite=False, live=False):
        self.id, self.filename, self.asset_date, self.created = id, filename, date, date
        self.size, self.is_live_photo, self._blob = len(blob), live, blob
        self.asset_record = {"fields": {"isFavorite": {"value": 1 if favorite else 0}}}
        self.versions = {"original_video": {"filename": filename.rsplit(".", 1)[0] + ".MOV"}} if live else {}
        self.deleted = False

    def download_url(self, version):
        return f"fake://{self.id}/{version}"

    def delete(self):
        self.deleted = True
        return True


class Album:
    def __init__(self, assets):
        self.photos = list(assets)


class Account:
    def __init__(self):
        a = [Asset("A1", "IMG_1.png", OLD, png_bytes(seed=911)),
             Asset("A2", "IMG_2.png", OLD, png_bytes(seed=912), favorite=True),
             Asset("A3", "IMG_3.png", NEW, png_bytes(seed=913)),
             Asset("A4", "IMG_4.png", OLD, png_bytes(seed=914), live=True),
             Asset("A5", "IMG_5.png", OLD, png_bytes(seed=915))]
        self.assets = {x.id: x for x in a}
        self.hidden = [Asset("H1", "IMG_H.png", OLD, png_bytes(seed=916))]
        self.albums = {"Library": Album(a), "Hidden": Album(self.hidden), "Trip": Album([a[0]])}
        self.all = self.albums["Library"]
        self.urls = {f"fake://{x.id}/original": x._blob for x in a + self.hidden}
        self.urls["fake://A4/original_video"] = MOV


class Session:
    def __init__(self, acct):
        self.acct = acct

    def get(self, url, stream=True, timeout=None):
        acct = self.acct
        class R:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def raise_for_status(s): pass
            def iter_content(s, n): yield acct.urls[url]
        return R()


STATE = {"trusted": False, "expired": False}
ACCOUNT = {}


class FakeService:
    def __init__(self, apple_id, password=None, cookie_directory=None):
        if STATE["expired"]:
            raise E.PyiCloudFailedLoginException("session expired")
        if password is None and not STATE["trusted"]:
            raise E.PyiCloudNoStoredPasswordAvailableException("no password")
        if password not in (None, "right"):
            raise E.PyiCloudFailedLoginException("bad password")
        self.requires_2fa = not STATE["trusted"]
        self.requires_2sa = False
        self.is_trusted_session = STATE["trusted"]
        self.photos = ACCOUNT["acct"]
        self.session = Session(ACCOUNT["acct"])

    def validate_2fa_code(self, code):
        return code == "123456"

    def trust_session(self):
        STATE["trusted"] = True
        return True


@pytest.fixture
def icloud(monkeypatch, tmp_path, app):
    STATE.update(trusted=False, expired=False)
    ACCOUNT["acct"] = Account()
    monkeypatch.setattr(apple_mod, "_pyicloud", lambda: (FakeService, E))
    old = app.state.get("icloud_session_dir")
    app.state["icloud_session_dir"] = str(tmp_path / "icloud")
    yield ACCOUNT["acct"]
    app.state["icloud_session_dir"] = old or "data/icloud"


def _sign_in(client, **cfg):
    r = client.post("/api/import/icloud/save", json={"config": {"apple_id": "ann@icloud.com", **cfg},
                                                     "secrets": {"password": "right"}})
    d = r.get_json()
    assert r.status_code == 200 and d["prompt"]["action"] == "code", d
    bad = client.post("/api/import/icloud/action", json={"id": d["id"], "action": "code", "code": "000000"})
    assert bad.status_code == 400
    ok = client.post("/api/import/icloud/action", json={"id": d["id"], "action": "code", "code": "123456"}).get_json()
    assert ok["signed_in"]
    return d["id"]


def test_icloud_sign_in_import_and_purge(client, app, imports, icloud):
    r = client.post("/api/import/icloud/save", json={"config": {"apple_id": "ann@icloud.com"}, "secrets": {"password": "no"}})
    assert r.status_code == 400 and "rejected" in r.get_json()["error"]
    sid = _sign_in(client, every_h=6, purge=True, keep_days=30, keep_favorites=True)
    src = app._db().execute("SELECT secrets, status FROM import_sources WHERE id=?", (sid,)).fetchone()
    assert "right" not in src["secrets"] and src["status"] == ""        # password never stored

    run_source(client, app, "icloud", sid)
    one = ledger(app, "icloud", "IMG_1.png")
    assert one["status"] == "done" and "Trip" in app._file_albums(one["rel_path"])
    assert "hidden" in read_meta(client, ledger(app, "icloud", "IMG_H.png")["rel_path"])["tags"]
    assert ledger(app, "icloud", "IMG_4.MOV")["status"] == "done"
    assert not any(a.deleted for a in icloud.assets.values())           # first run: still ingesting, nothing purged

    # the library copy of IMG_5 disappears: it must NOT be purged from iCloud
    os.remove(os.path.join(app.MEDIA_DIR, ledger(app, "icloud", "IMG_5.png")["rel_path"]))
    run_source(client, app, "icloud", sid)
    d = {k: a.deleted for k, a in icloud.assets.items()}
    assert d == {"A1": True, "A2": False, "A3": False, "A4": True, "A5": False}, d
    # A2 favourite kept, A3 too recent, A4 live photo purged only because its video is here too


def test_icloud_session_expiry_asks_to_sign_in(client, app, imports, icloud):
    sid = _sign_in(client)
    STATE["expired"] = True
    row = run_source(client, app, "icloud", sid)
    assert row["status"] in ("error", "pending") and "sign in again" in (row["error"] or "")
    assert app._db().execute("SELECT status FROM import_sources WHERE id=?", (sid,)).fetchone()["status"] == "needs sign-in"
    assert client.post("/api/import/icloud/run", json={"id": sid}).status_code == 409


def test_purge_needs_keep_days(client, imports, icloud):
    r = client.post("/api/import/icloud/save", json={"config": {"apple_id": "a@b.c", "purge": True, "keep_days": 0},
                                                     "secrets": {"password": "right"}})
    assert r.status_code == 400 and "keep at least 1 day" in r.get_json()["error"]
