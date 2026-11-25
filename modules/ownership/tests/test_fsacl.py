"""! @file
@brief Host ACLs (fsacl.py): translation to NTFS / NFSv4 and back, and push / import
through an in-memory disk (the "fake" backend; the Windows and NFSv4 backends need
that host). Off unless LDAP is enabled and configured."""
import os

import pytest

from modules.ownership import acl, fsacl

FI, DI = acl.FILE_INHERIT, acl.DIRECTORY_INHERIT


def test_flag_and_text_translation():
    f = FI | DI | acl.NO_PROPAGATE | acl.INHERITED | acl.SUCCESSFUL_ACCESS
    assert fsacl.from_nt_flags(fsacl.to_nt_flags(f)) == f
    assert fsacl.to_nt_flags(acl.INHERITED) == fsacl.NT_INHERITED == 0x10
    line = fsacl.nfs_line("allow", "proj@corp.example", acl.PRESETS["modify"], FI | DI, group=True)
    assert line.startswith("A:fdg:proj@corp.example:") and "C" not in line.split(":")[-1]
    (t, who, mask, flags, grp), = fsacl.parse_nfs("# file: x\n" + line + "\n")
    assert (t, who, mask, flags, grp) == ("allow", "proj@corp.example", acl.PRESETS["modify"], FI | DI, True)
    assert fsacl.parse_nfs("D:I:OWNER@:rwad")[0][3] == acl.INHERITED
    assert fsacl.cn_of("CN=Proj\\, Alpha,OU=Groups,DC=corp,DC=example") == "Proj, Alpha"
    assert fsacl.domain_from_base_dn("DC=corp,DC=example,DC=com") == "CORP"
    assert fsacl.domain_from_base_dn("DC=corp,DC=example,DC=com", netbios=False) == "corp.example.com"


@pytest.fixture
def disk(host, monkeypatch):
    """! @brief Host ACLs on, through the in-memory backend, with LDAP "enabled"."""
    h = host.get_service("acl")["host_acl"]
    saved = {k: host.config.get(k) for k in ("fs_acl", "fs_acl_backend", "fs_acl_domain", "fs_acl_service",
                                             "_fsacl_sync_jobs")}
    assert not h.status()["active"] and "LDAP" in h.status()["reason"]        # off without LDAP
    monkeypatch.setattr(h, "ldap_active", lambda: True)
    host.config.update({"fs_acl": "auto", "fs_acl_backend": "fake", "fs_acl_domain": "corp.example",
                        "fs_acl_service": "svc-cim@corp.example", "_fsacl_sync_jobs": True})
    h._backend, h._avail = None, None
    assert h.status()["active"]
    yield h
    host.config.update(saved)
    for k, v in saved.items():
        if v is None:
            host.config.pop(k, None)
    h._backend, h._avail = None, None


def _entries(h, rel):
    return h.backend().read(h.abs(rel), os.path.isdir(h.abs(rel)))


def test_push_and_import_round_trip(client, host, upload, disk):
    db = host.db()
    db.execute("INSERT OR IGNORE INTO auth_users(username, source, display_name) VALUES ('fs_ann','ldap','Ann')")
    db.commit()
    ann = db.execute("SELECT id FROM auth_users WHERE username='fs_ann'").fetchone()["id"]
    g = client.post("/api/auth/groups/create", json={"name": "fs_proj",
                                                     "ldap_dn": "CN=Proj,OU=Groups,DC=corp,DC=example"}).get_json()["group"]
    loc = client.post("/api/auth/users/create", json={"username": "fs_local", "password": "pw"}).get_json()["user"]
    rel = upload("fs_a.png", seed=9101, folder="fs_proj/sub")
    try:
        r = client.post("/api/acl/set", json={"path": "fs_proj", "kind": "dir", "protected": True, "entries": [
            {"type": "allow", "who": f"group:{g['id']}", "rights": "modify"},
            {"type": "allow", "who": f"user:{ann}", "rights": "read"},
            {"type": "allow", "who": f"user:{loc['id']}", "rights": "read"}]})
        assert r.status_code == 200, r.get_json()
        fold = _entries(disk, "fs_proj")
        hosts = {(e["host"], e["type"]) for e in fold["entries"]}
        assert ("Proj@corp.example", "allow") in hosts and ("fs_ann@corp.example", "allow") in hosts
        assert ("svc-cim@corp.example", "allow") in hosts                  # the server keeps access
        assert not any("fs_local" in e["host"] for e in fold["entries"])  # a local account can't be on disk
        assert any(e["group"] for e in fold["entries"] if e["host"] == "Proj@corp.example")
        # NFSv4-style: the files below carry the inherited entries, flagged INHERITED
        f = _entries(disk, rel)
        assert any(e["host"] == "Proj@corp.example" and e["flags"] & acl.INHERITED for e in f["entries"])
        d = client.get("/api/acl/disk", query_string={"path": "fs_proj", "kind": "dir"}).get_json()
        assert d["in_sync"] is True and d["app_only"] == ["fs_local"]

        # someone changes the folder on the host: a new directory account and a foreign one
        fold["entries"].append({"type": "deny", "host": "fs_newbie@corp.example", "mask": acl.PRESETS["read"],
                                "flags": FI | DI, "group": False})
        fold["entries"].append({"type": "allow", "host": "backup@other.example", "mask": acl.PRESETS["read"],
                                "flags": FI | DI, "group": False})
        disk.backend().disk[disk.abs("fs_proj")] = fold
        d = client.get("/api/acl/disk", query_string={"path": "fs_proj", "kind": "dir"}).get_json()
        assert d["in_sync"] is False and d["unmapped"] == ["backup@other.example"]
        r = client.post("/api/acl/disk/import", json={"path": "fs_proj", "kind": "dir"})
        assert r.status_code == 200, r.get_json()
        new = db.execute("SELECT id, source FROM auth_users WHERE username='fs_newbie'").fetchone()
        assert new is not None and new["source"] == "ldap"                # kept as a directory account
        whos = {e["who"] for e in r.get_json()["entries"]}
        assert f"user:{new['id']}" in whos and "host:backup@other.example" in whos
        assert r.get_json()["protected"] is True
        assert r.get_json()["disk"]["in_sync"] is True
        log = client.get("/api/acl/audit", query_string={"action": "acl.import"}).get_json()["entries"]
        assert any(e["path"] == "fs_proj" for e in log)

        # a full push lays the library root down with the path rules spelled out
        assert client.post("/api/acl/disk/push", json={}).get_json()["started"]
        root = _entries(disk, "")
        assert any(e["host"] == "EVERYONE@" and e["mask"] == acl.PRESETS["modify"] for e in root["entries"])
        assert client.get("/api/acl/disk", query_string={"path": "", "kind": "dir"}).get_json()["in_sync"] is True
    finally:
        client.post("/api/acl/set", json={"path": "fs_proj", "kind": "dir", "entries": []})
        client.post("/api/auth/groups/delete", json={"id": g["id"]})
        for name in ("fs_ann", "fs_newbie"):
            r = db.execute("SELECT id FROM auth_users WHERE username=?", (name,)).fetchone()
            if r:
                client.post("/api/auth/users/delete", json={"id": r["id"]})
        client.post("/api/auth/users/delete", json={"id": loc["id"]})


def test_windows_zone_root_keeps_file_only_entries(host, disk):
    """! @brief On NTFS users/<name>/ is protected: file-only entries from above go on it
    as inherit-only so the files below still get them."""
    pol = disk.policy
    pol.set_acl("", True, False, [{"type": "allow", "who": "group:999", "mask": acl.PRESETS["read"],
                                   "flags": FI}], push=False)
    try:
        ents, prot, _owner = disk.desired("users/somebody", True, "windows")
        assert prot
        assert any(e["who"] == "group:999" and e["flags"] == FI | acl.INHERIT_ONLY for e in ents)
        assert any(e["who"] == fsacl.INFRA_SERVICE for e in ents)
        ents, prot, _ = disk.desired("fs_any/file.jxl", False, "windows")
        assert not prot and ents == []          # Windows propagates: no own entries to write
    finally:
        pol.set_acl("", True, False, [], push=False)
