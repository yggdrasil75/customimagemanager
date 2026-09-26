"""metasrc hub: a fake photo source goes through search → apply → file tags."""
from cimtest import read_meta, post_json

from modules.metasrc.module import merge_fields


def test_merge_fields_fill_vs_overwrite():
    cur = {"title": "old", "album": "", "tags": ["a"]}
    assert merge_fields("music", cur, {"title": "new", "album": "x", "year": ""}, False) == {"album": "x"}
    assert merge_fields("music", cur, {"title": "new"}, True) == {"title": "new"}
    assert merge_fields("photo", cur, {"tags": ["a", "b"]}, False) == {"tags": ["a", "b"]}


def test_fake_source_roundtrip(client, host, upload):
    reg = host.get_service("metasrc")
    seen = {}
    reg.register({"id": "_fake", "label": "Fake", "kind": "photo", "priority": 99,
                  "search": lambda q: seen.update(q) or [
                      {"id": 1, "title": "hit", "subtitle": "", "thumb": None,
                       "fields": {"tags": ["from lookup"], "description": "found"}}],
                  "detail": lambda i: {"tags": ["detail tag"]}})
    fn = upload(seed=701)
    j = post_json(client, "/api/metasrc/search", {"kind": "photo", "rel_path": fn, "source": "_fake"})
    assert j["success"] and j["candidates"][0]["source"] == "_fake"
    assert len(seen["md5"]) == 32 and seen["abs_path"]
    c = j["candidates"][0]
    j = post_json(client, "/api/metasrc/apply", {"kind": "photo", "rel_path": fn, "source": "_fake",
                                                 "id": c["id"], "fields": c["fields"]})
    assert j["success"] and set(j["written"]) == {"tags", "description"}
    m = read_meta(client, fn)
    assert "from lookup" in m["tags"] and "detail tag" in m["tags"] and m["description"] == "found"
    assert any(s["id"] == "_fake" for s in client.get("/api/metasrc/sources").get_json()["sources"]["photo"])
