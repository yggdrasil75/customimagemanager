"""! @file
@brief Family share: a paired phone acts as its owner's account.

With sign-in on, a request authenticated by a phone's key runs as the account
that owns the phone: access policies (ownership) filter what it lists and
fetches, uploads land in the owner's personal folder, quotas and feature
permissions apply, is_admin follows the owner, a disabled owner is refused and
a deleted owner's phones go. Existing phones are given to the first admin."""
import json
import time

import pytest

from cimtest import png_bytes
from modules.family_share import crypto as fc
from modules.family_share import peer_client as pc

USER, OTHER, ADMIN, UPLOADER = "fsa_user", "fsa_other", "fsa_admin", "fsa_up"


@pytest.fixture(autouse=True)
def _no_network_polls(monkeypatch):
    """! @brief The worker would poll peers with a URL; tests have no network."""
    monkeypatch.setattr(pc, "mailbox_list", lambda *a, **k: [])


@pytest.fixture
def accounts(app, host):
    """! @brief Local accounts for the test; sign-in is turned on with on() and is off
    again (and the accounts and their phones gone) afterwards."""
    if host.get_service("ownership") is None:
        pytest.skip("the ownership module gives accounts their users/<name>/ tree")
    auth = app._authmgr
    specs = {ADMIN: {"is_admin": True},
             USER: {"role": "custom", "perms": {"data.upload": "write", "data.delete": "write", "tab.gallery": "read"}},
             OTHER: {"role": "custom", "perms": {"data.upload": "block"}},
             UPLOADER: {"role": "uploader"}}
    ids = {}
    for name, kw in specs.items():
        if auth.get_user(name) is None:
            auth.create_local_user(name, "pw", **kw)
        ids[name] = auth.get_user(name)["id"]
    app.state["auth"]["mode"] = "local"

    class Accounts(dict):
        """! @brief {username: id} plus on() to turn sign-in on."""
        def on(self):
            app.state["auth"]["enabled"] = True
    acc = Accounts(ids)
    try:
        yield acc
    finally:
        app.state["auth"]["enabled"] = False
        db = app._db()
        qm = ",".join("?" * len(ids))
        db.execute(f"DELETE FROM fs_peers WHERE kind='device' AND user_id IN ({qm})", list(ids.values()))
        db.execute("DELETE FROM fs_received"); db.commit()
        for name in ids:
            u = auth.get_user(name)
            if u is not None:
                auth.delete_user(u["id"])


def _login(app, user):
    """! @brief A fresh client signed in as `user` -> (client, CSRF headers)."""
    c = app.app.test_client()
    r = c.post("/api/auth/login", json={"username": user, "password": "pw"})
    assert r.status_code == 200, r.get_json()
    return c, {"X-CSRF-Token": r.get_json()["csrf"]}


def _pair(app, user, name, **extra):
    """! @brief `user` pairs a phone in My devices (both codes exchanged); returns what the
    phone holds: its private key, the server's key and the headers it sends."""
    c, csrf = _login(app, user)
    priv = fc.generate_private_key()
    code = fc.make_pairing_code(name, "", fc.public_key(priv), "phone-secret", "dev-" + name)
    r = c.post("/api/family_share/devices/save", headers=csrf, json={"name": name, "pairing_code": code, **extra})
    j = r.get_json()
    assert r.status_code == 200 and j["ok"], j
    k = c.post("/api/family_share/devices/key", headers=csrf, json={"id": j["id"]}).get_json()
    me = fc.parse_pairing_code(k["pairing_code"])
    return {"id": j["id"], "name": name, "priv": priv, "my_pub": me["pub_key"], "my_id": me["instance_id"],
            "headers": {pc.HEADER_PEER: me["my_name"], pc.HEADER_KEY: me["key_out"]}}


def _open(phone, r):
    assert r.status_code == 200, (r.status_code, r.get_data(as_text=True)[:300])
    opener = fc.Opener(phone["priv"], phone["my_pub"], json.loads(r.headers["X-Family-Env"]))
    return opener.open_bytes(r.get_data())


def _timeline(app, phone):
    r = app.app.test_client().get("/api/family_share/inbound/timeline?limit=2000", headers=phone["headers"])
    return {f["p"] for f in json.loads(_open(phone, r))["files"]}


def _push(app, phone, seed, sha, folder="DCIM", tmp=None):
    sealer = fc.Sealer(phone["priv"], phone["my_pub"])
    inner = {"ts": time.time(), "to": phone["my_id"], "origin_sha": sha, "origin_id": "dev-" + phone["name"],
             "folder": folder, "orig_name": f"{sha}.png", "metadata": {"tags": []}}
    src = tmp / f"{sha}.src"; src.write_bytes(png_bytes(seed=seed))
    enc = tmp / f"{sha}.enc"; sealer.seal_file(str(src), str(enc))
    data = {"env": json.dumps(sealer.header), "meta": sealer.seal_meta(inner),
            "file": (open(enc, "rb"), "payload.bin")}
    return app.app.test_client().post("/api/family_share/inbound/push", headers=phone["headers"],
                                      content_type="multipart/form-data", data=data)


def _ping(app, phone):
    return app.app.test_client().get("/api/family_share/inbound/ping", headers=phone["headers"])


def test_phone_lists_only_what_its_owner_sees(app, client, upload, accounts):
    pub = upload(seed=1101, folder="fsa_pub")
    mine = upload(seed=1102, folder=f"users/{USER}/fsa")
    other = upload(seed=1103, folder=f"users/{OTHER}/fsa")
    accounts.on()
    personal = _pair(app, USER, "fsa-pixel")
    everything = _pair(app, USER, "fsa-tablet", scope="all")

    seen = _timeline(app, personal)
    assert mine in seen and pub not in seen and other not in seen
    seen = _timeline(app, everything)
    assert mine in seen and pub in seen and other not in seen

    thumb = lambda ph, rel: app.app.test_client().get(f"/api/family_share/inbound/thumb?p={rel}", headers=ph["headers"])
    assert thumb(personal, mine).status_code == 200
    assert thumb(personal, pub).status_code == 404          # outside the personal scope
    assert thumb(everything, pub).status_code == 200
    assert thumb(everything, other).status_code == 404      # another user's tree
    media = app.app.test_client().get(f"/api/family_share/inbound/media?p={other}", headers=everything["headers"])
    assert media.status_code == 404

    acct = _ping(app, personal).get_json()["account"]
    assert acct == {"username": USER, "display_name": USER, "scope": "personal", "is_admin": False}
    assert _ping(app, everything).get_json()["account"]["scope"] == "all"


def test_upload_lands_in_owner_folder_with_owner_permissions(app, host, accounts, tmp_path):
    accounts.on()
    phone = _pair(app, USER, "fsa-cam")
    cam = _pair(app, USER, "fsa-cam2", scope="all", folder="fsa_public_cam")
    jailed = _pair(app, USER, "fsa-cam3", folder="fsa_public_cam")         # personal: may not write there
    seen = []
    probe = lambda **kw: seen.append(host.is_admin())
    host.on("upload.check", probe)
    made = []
    try:
        r = _push(app, phone, 1110, "fsa1", tmp=tmp_path)
        j = r.get_json(); made.append(j.get("filename"))
        assert r.status_code == 200 and j["stored"], j
        assert j["filename"].startswith(f"users/{USER}/phone/fsa-cam/DCIM/"), j["filename"]
        assert seen == [False]                                           # is_admin follows the owner
        j = _push(app, cam, 1111, "fsa2", tmp=tmp_path).get_json(); made.append(j.get("filename"))
        assert j["filename"].startswith("fsa_public_cam/DCIM/"), j
        j = _push(app, jailed, 1112, "fsa3", tmp=tmp_path).get_json(); made.append(j.get("filename"))
        assert j["filename"].startswith(f"users/{USER}/phone/fsa-cam3/"), j

        # an admin's phone: admin only with scope "all"
        seen.clear()
        adm = _pair(app, ADMIN, "fsa-admin-all", scope="all")
        j = _push(app, adm, 1113, "fsa4", tmp=tmp_path).get_json(); made.append(j.get("filename"))
        assert seen == [True] and _ping(app, adm).get_json()["account"]["is_admin"] is True
        adm_p = _pair(app, ADMIN, "fsa-admin-personal")
        assert _ping(app, adm_p).get_json()["account"]["is_admin"] is False

        # an uploader's phone uploads but neither browses nor deletes
        up = _pair(app, UPLOADER, "fsa-up")
        j = _push(app, up, 1114, "fsa5", tmp=tmp_path).get_json(); made.append(j.get("filename"))
        assert j["ok"] and j["filename"].startswith(f"users/{UPLOADER}/"), j
        r = app.app.test_client().get("/api/family_share/inbound/timeline", headers=up["headers"])
        assert r.status_code == 403 and r.get_json()["error_code"] == "forbidden"
        sealer = fc.Sealer(up["priv"], up["my_pub"])
        r = app.app.test_client().post("/api/family_share/inbound/revoke", headers=up["headers"], json={
            "env": sealer.header, "meta": sealer.seal_meta({"ts": time.time(), "to": up["my_id"], "origin_sha": "fsa5"})})
        assert r.status_code == 403
        # a user without upload rights: refused
        no = _pair(app, OTHER, "fsa-other")
        assert _push(app, no, 1115, "fsa6", tmp=tmp_path).status_code == 403
    finally:
        host.event_hooks["upload.check"].remove(probe)
        app.state["auth"]["enabled"] = False
        for fn in made:
            if fn:
                app.app.test_client().post("/api/delete", json={"filename": fn})


def test_owner_quota_refuses_oversized_upload(app, host, accounts, tmp_path):
    before = host.config.get("quota_default_gb")
    accounts.on()
    phone = _pair(app, USER, "fsa-quota")
    host.set_config("quota_default_gb", 0.000001, save=False)               # ~1 KB
    try:
        r = _push(app, phone, 1120, "fsaq", tmp=tmp_path)
        j = r.get_json()
        assert r.status_code == 413 and j["error_code"] == "refused" and "quota" in j["error"].lower(), j
        assert app._db().execute("SELECT COUNT(*) FROM fs_received WHERE origin_sha='fsaq'").fetchone()[0] == 0
    finally:
        host.set_config("quota_default_gb", before, save=False)


def test_disabled_or_deleted_owner_is_refused(app, accounts):
    auth = app._authmgr
    accounts.on()
    phone = _pair(app, OTHER, "fsa-dis")
    assert _ping(app, phone).status_code == 200
    auth.update_user(accounts[OTHER], disabled=True)
    r = _ping(app, phone)
    assert r.status_code == 403 and r.get_json()["error_code"] == "account_disabled", r.get_json()
    assert OTHER in r.get_json()["error"]
    auth.update_user(accounts[OTHER], disabled=False)
    assert _ping(app, phone).status_code == 200
    # deleting the account deletes its phones at once (user.deleted event) ...
    auth.delete_user(accounts[OTHER])
    assert app._db().execute("SELECT COUNT(*) FROM fs_peers WHERE id=?", (phone["id"],)).fetchone()[0] == 0
    assert _ping(app, phone).status_code == 401


def test_devices_are_per_user_and_admin_assigns_owners(app, accounts):
    accounts.on()
    mine = _pair(app, USER, "fsa-own")
    c, csrf = _login(app, OTHER)
    lst = c.get("/api/family_share/devices").get_json()
    assert lst["ok"] and all(d["name"] != "fsa-own" for d in lst["devices"])
    # another user's phone is out of reach, and a user can't own a phone for someone else
    assert c.post("/api/family_share/devices/delete", headers=csrf, json={"id": mine["id"]}).status_code == 404
    assert c.post("/api/family_share/devices/key", headers=csrf, json={"id": mine["id"]}).status_code == 404
    j = c.post("/api/family_share/devices/save", headers=csrf,
               json={"name": "fsa-sneaky", "user_id": accounts[USER]}).get_json()
    assert j["ok"]
    row = app._db().execute("SELECT user_id FROM fs_peers WHERE id=?", (j["id"],)).fetchone()
    assert row["user_id"] == accounts[OTHER]
    # instance peering stays admin only
    assert c.get("/api/family_share/state").status_code == 403
    assert c.post("/api/family_share/peers/save", headers=csrf, json={"name": "x"}).status_code == 403
    # the admin pairs a phone for a user and sees owners in the state
    a, acsrf = _login(app, ADMIN)
    j = a.post("/api/family_share/peers/save", headers=acsrf,
               json={"name": "fsa-for-user", "kind": "device", "user_id": accounts[USER], "scope": "all"}).get_json()
    assert j["ok"] and j["peer"]["owner"] == USER and j["peer"]["scope"] == "all" and not j["peer"]["owner_auto"]
    st = a.get("/api/family_share/state").get_json()
    assert any(u["username"] == USER for u in st["users"])
    u, _ = _login(app, USER)
    names = {d["name"] for d in u.get("/api/family_share/devices").get_json()["devices"]}
    assert {"fsa-own", "fsa-for-user"} <= names
    # family instances can't browse the library
    sis = fc.generate_private_key()
    pj = a.post("/api/family_share/peers/save", headers=acsrf,
                json={"name": "fsa-sis", "pub_key": fc.public_key(sis), "url": "http://sis.test"}).get_json()
    code = fc.parse_pairing_code(a.post("/api/family_share/peers/key", headers=acsrf, json={"id": pj["id"]}).get_json()["pairing_code"])
    try:
        r = app.app.test_client().get("/api/family_share/inbound/timeline",
                                      headers={pc.HEADER_PEER: "fsa-sis", pc.HEADER_KEY: code["key_out"]})
        assert r.status_code == 403
    finally:
        a.post("/api/family_share/peers/delete", headers=acsrf, json={"id": pj["id"]})


def test_migration_gives_existing_phones_to_an_admin(app, host, accounts):
    db = app._db()
    db.execute("INSERT INTO fs_peers(name, url, key_out, key_in, enabled, created, kind, folder, user_id, scope) "
               "VALUES ('fsa-legacy', '', '', 'k', 1, 0, 'device', '', 0, '')")
    db.commit()
    check = next(t["check"] for t in host.db_tables if t["module_id"] == "family_share")
    check(db)
    first = db.execute("SELECT id FROM auth_users WHERE is_admin=1 AND disabled=0 ORDER BY id LIMIT 1").fetchone()["id"]
    row = db.execute("SELECT * FROM fs_peers WHERE name='fsa-legacy'").fetchone()
    try:
        assert row["user_id"] == first and row["owner_auto"] == 1
        # an app paired before owners keeps its folder and its whole-library view
        assert row["folder"] == "phone/fsa-legacy" and row["scope"] == "all"
        peers = {p["name"]: p for p in app.app.test_client().get("/api/family_share/state").get_json()["peers"]}
        assert peers["fsa-legacy"]["owner_auto"] is True and peers["fsa-legacy"]["owner"]
    finally:
        db.execute("DELETE FROM fs_peers WHERE name='fsa-legacy'"); db.commit()
