"""! @file
@brief Owners, folder / file ACLs, multiple groups, the "user" role, the acl.manage
gate and the permission audit log, end to end through the HTTP API. Auth is off in
the test app (requests are the anonymous admin); `as_` runs a block as a real
non-admin account, with its real groups."""
import io
import os

import pytest

import features
from cimtest import png_bytes, signed_in_as


@pytest.fixture(scope="module")
def team(client):
    """! @brief alice + carol in group acl_proj, bob in no group; removed afterwards."""
    g = client.post("/api/auth/groups/create", json={"name": "acl_proj", "role": "user"}).get_json()["group"]
    g2 = client.post("/api/auth/groups/create", json={"name": "acl_other", "role": "viewer"}).get_json()["group"]
    out = {"group": g, "group2": g2}
    for name, gids in (("acl_alice", [g["id"], g2["id"]]), ("acl_bob", []), ("acl_carol", [g["id"]])):
        r = client.post("/api/auth/users/create", json={"username": name, "password": "pw", "role": "custom",
                                                        "group_ids": gids})
        assert r.status_code == 200, r.get_json()
        out[name] = r.get_json()["user"]
    yield out
    for k in ("acl_alice", "acl_bob", "acl_carol"):
        client.post("/api/auth/users/delete", json={"id": out[k]["id"]})
    for k in ("group", "group2"):
        client.post("/api/auth/groups/delete", json={"id": out[k]["id"]})


def as_(app, acct, **perms):
    feats = features.effective_permissions("custom", {"data.delete": "write", "data.upload": "write", **perms})
    return signed_in_as(app, {**acct, "is_admin": False, "features": feats})


def _names(client, **q):
    j = client.get("/api/list", query_string=q).get_json()
    assert j["success"], j
    return {f["filename"] for f in j["files"]}


def _up(client, name, seed, folder="", **form):
    r = client.post("/api/upload", data={"file": (io.BytesIO(png_bytes(seed=seed)), name), "mode": "sync",
                                         "folder": folder, **form}, content_type="multipart/form-data")
    return r


def test_multiple_groups_and_user_role(client, team):
    alice = team["acl_alice"]
    assert sorted(alice["group_ids"]) == sorted([team["group"]["id"], team["group2"]["id"]])
    assert {g["name"] for g in alice["groups"]} == {"acl_proj", "acl_other"}
    # a custom account takes the most permissive of its groups' roles
    assert alice["effective_role"] == "user"
    assert "user" in client.get("/api/auth/features").get_json()["roles"]
    # the user role: features yes, server settings tabs no
    perms = features.effective_permissions("user", {})
    assert features.has_level(perms, "data.upload", "write")
    assert not features.has_level(perms, "settings.general", "read")
    assert features.has_level(features.effective_permissions("viewer", {}), "acl.manage", "read") is False
    # group overrides merge: the most permissive wins
    assert features.merge_group_overrides([{"x.y": "read"}, {"x.y": "write"}, {"x.y": "default"}]) == {"x.y": "write"}
    # membership edits from the group side
    gid = team["group2"]["id"]
    client.post("/api/auth/groups/update", json={"id": gid, "members": [team["acl_bob"]["id"]]})
    users = {u["username"]: u for u in client.get("/api/auth/users").get_json()["users"]}
    assert gid in users["acl_bob"]["group_ids"] and gid not in users["acl_alice"]["group_ids"]
    client.post("/api/auth/users/update", json={"id": alice["id"], "group_ids": alice["group_ids"]})
    client.post("/api/auth/users/update", json={"id": team["acl_bob"]["id"], "group_ids": []})


def test_project_folder_acl_end_to_end(app, client, team, upload):
    alice, bob, carol = team["acl_alice"], team["acl_bob"], team["acl_carol"]
    gid = team["group"]["id"]
    secret = upload("plan.png", seed=3101, folder="acl_proj")       # admin puts a file in the project
    pub = upload("pub.png", seed=3102, folder="acl_pub")
    # admin closes the project folder to everyone but the group
    r = client.post("/api/acl/set", json={"path": "acl_proj", "kind": "dir", "protected": True, "entries": [
        {"type": "allow", "who": f"group:{gid}", "rights": "modify", "applies_to": "this_subfolders_files"},
        {"type": "allow", "who": "OWNER@", "rights": "full", "applies_to": "subfolders_files"},
        {"type": "audit", "who": "EVERYONE@", "rights": "read", "flags": ["failure"]}]})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["protected"] and len(r.get_json()["entries"]) == 3

    with as_(app, bob):
        assert secret not in _names(client) and pub in _names(client)
        assert client.get(f"/api/thumb/{secret}").status_code != 200
        assert client.get(f"/api/file/{secret}").status_code != 200
        client.post("/api/delete", json={"filename": secret})
        r = _up(client, "sneak.png", 3103, folder="acl_proj")
        assert r.status_code >= 400, r.get_json()
        assert client.get("/api/acl", query_string={"path": secret}).status_code == 403
    assert os.path.exists(os.path.join("media", secret))
    assert not os.path.exists(os.path.join("media", "acl_proj", "sneak.jxl"))

    with as_(app, carol):
        assert secret in _names(client) and pub in _names(client)
        assert client.get(f"/api/thumb/{secret}").status_code == 200
        # carol can't change the folder's permissions (no change-permissions right)
        assert client.post("/api/acl/set", json={"path": "acl_proj", "kind": "dir", "entries": []}).status_code == 403

    with as_(app, alice):
        r = _up(client, "alice_doc.png", 3104, folder="acl_proj")
        assert r.status_code == 200, r.get_json()
        mine = r.get_json()["filename"]
        upload.made.append(mine)
        info = client.get("/api/acl", query_string={"path": mine}).get_json()
        assert info["owner"]["username"] == "acl_alice" and info["owner_source"] == "upload"
        assert info["can"]["write_acl"]
        assert any(e["source"] == "acl_proj" for e in info["inherited"])
        # alice opens her file to bob, read only
        r = client.post("/api/acl/set", json={"path": mine, "entries": [
            {"type": "allow", "who": f"user:{bob['id']}", "rights": "read"}]})
        assert r.status_code == 200, r.get_json()
        eff = client.get("/api/acl/effective", query_string={"path": mine, "user_id": bob["id"]}).get_json()
        assert "read_data" in eff["rights"] and "delete" not in eff["rights"]
    with as_(app, bob):
        assert mine in _names(client) and secret not in _names(client)
        client.post("/api/delete", json={"filename": mine})
    assert os.path.exists(os.path.join("media", mine))

    # a deny entry without protection leaves the rest of the library alone
    client.post("/api/acl/set", json={"path": "acl_pub", "kind": "dir", "entries": [
        {"type": "deny", "who": f"user:{bob['id']}", "rights": "read"}]})
    with as_(app, bob):
        assert pub not in _names(client)
    with as_(app, carol):
        assert pub in _names(client)
    client.post("/api/acl/set", json={"path": "acl_pub", "kind": "dir", "entries": []})
    with as_(app, bob):
        assert pub in _names(client)

    # the audit log saw the change, the upload and bob's refused attempts
    log = client.get("/api/acl/audit", query_string={"limit": 500}).get_json()["entries"]
    acts = {(e["action"], e["path"]) for e in log}
    assert ("acl.set", "acl_proj") in acts and ("upload", mine) in acts
    assert any(e["action"] == "access.denied" and e["username"] == "acl_bob" and not e["ok"] for e in log)
    csv = client.get("/api/acl/audit", query_string={"format": "csv", "path": "acl_proj"})
    assert csv.status_code == 200 and b"acl.set" in csv.data
    client.post("/api/acl/set", json={"path": "acl_proj", "kind": "dir", "entries": []})


def test_private_upload_default_and_scopes(app, client, team, upload):
    alice, bob = team["acl_alice"], team["acl_bob"]
    with as_(app, bob):
        assert client.get("/api/acl/me").get_json()["default_scope"] == "public"
        r = _up(client, "bob_private.png", 4101, folder="acl_misc", scope="private")
        assert r.status_code == 200, r.get_json()
        priv = r.get_json()["filename"]
        upload.made.append(priv)
        assert not priv.startswith("users/") and priv in _names(client)
    with as_(app, alice):
        assert priv not in _names(client)
        assert client.get(f"/api/thumb/{priv}").status_code != 200
    # a viewer may not keep a personal folder: its "personal" upload becomes private
    viewer = {**bob, "role": "viewer", "effective_role": "viewer"}
    with as_(app, viewer):
        r = _up(client, "viewer_pers.png", 4102, scope="personal")
        assert r.status_code == 200, r.get_json()
        vp = r.get_json()["filename"]
        upload.made.append(vp)
        assert not vp.startswith("users/")
        assert "personal" not in client.get("/api/acl/me").get_json()["scopes"]
    assert client.get("/api/acl", query_string={"path": vp}).get_json()["protected"]


def test_public_links_follow_anonymous(app, client, upload):
    rel = upload("acl_link.png", seed=5101, folder="acl_links")
    ok = client.post("/api/shared_links/create", json={"kind": "files", "files": [rel]})
    if ok.status_code == 404:
        pytest.skip("shared_links not loaded")
    assert ok.status_code == 200, ok.get_json()
    token = ok.get_json()["link"]["token"]
    client.post("/api/acl/set", json={"path": rel, "protected": True, "entries": [
        {"type": "allow", "who": "AUTHENTICATED@", "rights": "read"}]})
    r = client.post("/api/shared_links/create", json={"kind": "files", "files": [rel]})
    assert r.status_code == 403
    info = client.get(f"/api/shared_links/pub/{token}/info").get_json()
    assert info["count"] == 0                       # the existing link no longer shows it
    client.post("/api/acl/set", json={"path": rel, "protected": True, "entries": [
        {"type": "allow", "who": "AUTHENTICATED@", "rights": "read"},
        {"type": "allow", "who": "ANONYMOUS@", "rights": "read"}]})
    assert client.get(f"/api/shared_links/pub/{token}/info").get_json()["count"] == 1
    client.post("/api/shared_links/delete", json={"token": token})


def test_acl_controls_hidden_without_the_feature(app, client, team, upload):
    rel = upload("acl_gate.png", seed=6101)
    with as_(app, team["acl_carol"], **{"acl.manage": "block"}):
        assert client.get("/api/acl", query_string={"path": rel}).status_code == 403
    with as_(app, team["acl_carol"], **{"acl.manage": "read"}):
        j = client.get("/api/acl", query_string={"path": rel}).get_json()
        assert j["success"] and not j["can"]["write_acl"]
        assert client.post("/api/acl/set", json={"path": rel, "entries": []}).status_code == 403


def test_owner_follows_moves_and_take_ownership(app, client, team, upload):
    alice, carol = team["acl_alice"], team["acl_carol"]
    with as_(app, alice):
        r = _up(client, "move_me.png", 7101, folder="acl_mv")
        rel = r.get_json()["filename"]
    r = client.post("/api/acl/owner", json={"path": rel, "user_id": carol["id"]})
    assert r.status_code == 200 and r.get_json()["owner"]["username"] == "acl_carol"
    with as_(app, alice):        # no longer the owner, no take-ownership right
        assert client.post("/api/acl/owner", json={"path": rel, "user_id": alice["id"]}).status_code == 403
    host_acl = app.module_host.get_service("acl")
    host_acl["set_owner"](rel, alice["id"])
    app.module_host.emit("file.renamed", old_rel=rel, new_rel="acl_mv/moved.jxl")
    assert host_acl["owner_id"]("acl_mv/moved.jxl") == alice["id"]
    app.module_host.emit("file.renamed", old_rel="acl_mv/moved.jxl", new_rel=rel)
    upload.made.append(rel)
