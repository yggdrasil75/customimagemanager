"""! @file
@brief Revision history: new version, list, download, restore, delete, permissions."""
import io
import os

import pytest

import features
from cimtest import signed_in_as


def _bytes(rel):
    with open(os.path.join("media", rel), "rb") as fh:
        return fh.read()


def _new_version(client, rel, data, comment="", name=None):
    return client.post("/api/revisions/upload", data={
        "path": rel, "comment": comment, "file": (io.BytesIO(data), name or os.path.basename(rel))},
        content_type="multipart/form-data")


def test_versions_round_trip(client, upload, host):
    if not host.has_service("revisions"):
        pytest.skip("revisions not loaded")
    rel = upload("rev_doc.png", seed=8101, folder="rev")
    other = upload("rev_other.png", seed=8102, folder="rev")
    v1, v2 = _bytes(rel), _bytes(other)
    assert client.get("/api/revisions", query_string={"path": rel}).get_json()["versions"] == []

    assert _new_version(client, rel, v2, name="x.png").status_code == 400          # wrong extension
    r = _new_version(client, rel, v2, "second draft")
    assert r.status_code == 200, r.get_json()
    vs = r.get_json()["versions"]
    assert [v["rev"] for v in vs] == [2, 1] and vs[0]["current"] and vs[1]["comment"] == "original"
    assert _bytes(rel) == v2
    assert _new_version(client, rel, v2).status_code == 400                        # unchanged

    d = client.get(f"/api/revisions/{vs[1]['id']}/download")
    assert d.status_code == 200 and d.data == v1
    r = client.post("/api/revisions/restore", json={"id": vs[1]["id"]})
    assert r.status_code == 200, r.get_json()
    vs = r.get_json()["versions"]
    assert vs[0]["rev"] == 3 and vs[0]["comment"] == "restored v1" and _bytes(rel) == v1
    # an earlier version can go; the current one can't
    assert client.post("/api/revisions/delete", json={"id": vs[0]["id"]}).status_code == 400
    r = client.post("/api/revisions/delete", json={"id": vs[1]["id"]})
    assert [v["rev"] for v in r.get_json()["versions"]] == [3, 1]
    # the history follows a rename
    host.emit("file.renamed", old_rel=rel, new_rel="rev/renamed.jxl")
    assert host.db().execute("SELECT COUNT(*) n FROM file_revisions WHERE rel_path='rev/renamed.jxl'").fetchone()["n"] == 2
    host.emit("file.renamed", old_rel="rev/renamed.jxl", new_rel=rel)


def test_versions_need_write_on_the_file(app, client, upload, host):
    if not host.has_service("revisions") or not host.has_service("acl"):
        pytest.skip("revisions / ownership not loaded")
    rel = upload("rev_locked.png", seed=8103, folder="rev_locked")
    client.post("/api/acl/set", json={"path": rel, "protected": True, "entries": [
        {"type": "allow", "who": "AUTHENTICATED@", "rights": "read"}]})
    u = client.post("/api/auth/users/create", json={"username": "rev_reader", "password": "pw"}).get_json()["user"]
    try:
        with signed_in_as(app, {**u, "is_admin": False,
                                "features": features.effective_permissions("custom", {})}):
            assert client.get("/api/revisions", query_string={"path": rel}).status_code == 200
            r = _new_version(client, rel, b"\xff\x0a" + b"x" * 64)
            assert r.status_code == 404
    finally:
        client.post("/api/auth/users/delete", json={"id": u["id"]})
        client.post("/api/acl/set", json={"path": rel, "entries": []})
