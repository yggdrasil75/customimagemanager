"""Tier objects know their home through XMP: a file's sidecar carries
xmpMM:DocumentID, the object is stored under that id, a lost symlink is
relinked at the sidecar's path, the object store is never indexed as library
content, and GC never eats an object whose sidecar still exists."""
import os, time
import tiering


def test_object_store_restore(app, client, upload, tmp_path):
    media = os.path.abspath(app.MEDIA_DIR)
    tier = tmp_path / "tier0"
    saved = {k: tiering._state.get(k) for k in ("cfg", "media_dir", "read_document_id", "ensure_document_id")}
    tiering._state.update(cfg={**tiering.DEFAULT_CFG, "enabled": True,
                               "tiers": [{"name": "t", "path": str(tier), "ratio": 100, "speed_mbps": 500}]},
                          media_dir=media, read_document_id=app._document_id,
                          ensure_document_id=app._ensure_document_id)
    try:
        fn = upload("tier_me.png", seed=21, scope="public", folder="trips/2026")
        link = os.path.join(media, fn)
        xmp = os.path.splitext(link)[0] + ".xmp"
        assert tiering._execute_move({"rel": fn, "to": 0, "size": 1}, tiering._state["cfg"], mbps=0)
        assert os.path.islink(link)
        obj = os.path.realpath(link)
        doc_id = app._document_id(xmp)
        assert doc_id and os.path.basename(obj) == doc_id + ".jxl"
        assert tiering.is_object_path("cim-objects/ab/abcd.jxl") and not tiering.is_object_path(fn)
        # a sidecar rewrite (tag edit) keeps the id
        client.post("/api/metadata", json={"filename": fn, "action": "write", "tags": ["kept"],
                                            "description": "", "regions": []})
        assert app._document_id(xmp) == doc_id

        # lose the symlink (a rebuilt media dir): the object is an orphan …
        os.remove(link)
        # … GC leaves it alone, even when old enough to collect …
        os.utime(obj, (time.time() - 7200, time.time() - 7200))
        assert tiering.gc_orphans() == 0 and os.path.exists(obj)
        # … and restore puts the link back where the sidecar is.
        assert tiering.restore_orphans() == [fn]
        assert os.path.islink(link) and os.path.realpath(link) == obj
        assert tiering.restore_orphans() == []           # idempotent

        # an object from before ids were stored under a random name: adopt_ids
        # gives the sidecar that name as its DocumentID
        other = upload("tier_old.png", seed=22, scope="public", folder="trips/2026")
        olink = os.path.join(media, other)
        oobj = os.path.join(str(tier), tiering.OBJECT_DIR, "ab", "abcdef0123456789abcdef0123456789.jxl")
        os.makedirs(os.path.dirname(oobj)); os.replace(olink, oobj); os.symlink(oobj, olink)
        assert tiering.adopt_ids() == 1
        assert app._document_id(os.path.splitext(olink)[0] + ".xmp") == "abcdef0123456789abcdef0123456789"
        os.remove(olink)
        assert tiering.restore_orphans() == [other]

        # a file lost BEFORE ids existed: random object name, symlink gone, no
        # DocumentID. The thumbnail cache (mtime, then aHash) finds its home.
        lost = upload("tier_lost.png", seed=23, scope="public", folder="trips/2026")
        llink = os.path.join(media, lost)
        client.get(f"/api/thumb/{lost}")                     # populate the thumb cache
        lobj = os.path.join(str(tier), tiering.OBJECT_DIR, "cd", "cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd.jxl")
        os.makedirs(os.path.dirname(lobj)); os.replace(llink, lobj)
        plan = client.post("/api/tiers/recover", json={}).get_json()
        assert [m["rel_path"] for m in plan["matched"]] == [lost] and plan["matched"][0]["how"] == "mtime"
        assert not os.path.lexists(llink)                    # a plan changes nothing
        os.utime(lobj, (time.time() - 9999, time.time() - 9999))   # mtime gone too → thumbnail hash
        plan = client.post("/api/tiers/recover", json={}).get_json()
        assert plan["matched"][0]["how"] == "thumbnail"
        done = client.post("/api/tiers/recover", json={"apply": True}).get_json()
        assert done["relinked"] == [lost] and os.path.realpath(llink) == lobj
        assert app._document_id(llink) == "cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd"
        assert client.post("/api/tiers/recover", json={}).get_json()["matched"] == []

        # a tier store placed under MEDIA_DIR is never walked as library content
        inside = os.path.join(media, tiering.OBJECT_DIR, "zz")
        os.makedirs(inside, exist_ok=True)
        stray = os.path.join(inside, "deadbeef.jxl")
        with open(stray, "wb") as f:
            f.write(b"x")
        try:
            assert not any(tiering.is_object_path(r) for r in app._enumerate_library())
        finally:
            os.remove(stray); os.removedirs(inside)
    finally:
        tiering._state.update(saved)