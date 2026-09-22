"""Dedup module: a resized / re-encoded copy is grouped with its original.
(Upload-time dedupe is sha256-only and is tested in tests/test_api.py.)"""
import pytest
from cimtest import post_json


def test_near_duplicate_pair_grouped(client, upload):
    a = upload.media("near_dup_a.jpg")
    b = upload.media("near_dup_b.jpg")
    o = upload.media("no_person.jpg")
    try:
        j = post_json(client, "/api/dedup", {"force": True})
        assert j["success"], j
        groups = client.get("/api/dedup_groups", query_string={"page": 0, "page_size": 200}).get_json()
        assert groups["success"]
        found = [{it["filename"] for it in g["items"]} for g in groups["groups"]]
        assert any({a, b} <= g for g in found), f"near-dup pair not grouped: {found}"
        assert not any({a, o} <= g for g in found), "unrelated photo grouped with near_dup_a"
    finally:
        client.post("/api/dedup_clear", json={})


def test_status_and_clear(client):
    assert client.get("/api/dedup_status").status_code in (200, 503)
    r = client.post("/api/dedup_clear", json={})
    assert r.status_code in (200, 503)
