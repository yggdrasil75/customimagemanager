"""! @file
@brief panorama: GPano / aspect detection, the file enricher and the settings route.
    ./run_tests.sh modules/panorama
"""
from modules.panorama import module as pm


def test_gpano_full_sphere():
    p = pm.pano_from_tags({"ProjectionType": "equirectangular", "InitialViewHeadingDegrees": "90"}, 1000, 800)
    assert p and p["source"] == "gpano" and p["projection"] == "equirectangular"
    assert p["crop_w"] is None and p["heading"] == 90.0


def test_gpano_partial_sphere():
    tags = {"ProjectionType": "equirectangular", "FullPanoWidthPixels": "8000", "FullPanoHeightPixels": "4000",
            "CroppedAreaImageWidthPixels": "6000", "CroppedAreaImageHeightPixels": "2000",
            "CroppedAreaLeftPixels": "1000", "CroppedAreaTopPixels": "900"}
    p = pm.pano_from_tags(tags, 6000, 2000)
    assert (p["full_w"], p["full_h"], p["crop_w"], p["crop_h"], p["crop_left"], p["crop_top"]) == (8000, 4000, 6000, 2000, 1000, 900)


def test_use_panorama_viewer_alone_counts():
    assert pm.pano_from_tags({"UsePanoramaViewer": "True"}, 100, 100)["source"] == "gpano"
    assert pm.pano_from_tags({"UsePanoramaViewer": "False"}, 100, 100, auto=False) is None


def test_aspect_heuristic():
    assert pm.pano_from_tags({}, 4000, 2000)["source"] == "aspect"
    assert pm.pano_from_tags({}, 4000, 2001) is None
    assert pm.pano_from_tags({}, 4000, 2001, aspect_min=1.9)["source"] == "aspect"
    assert pm.pano_from_tags({}, 4000, 2000, auto=False) is None
    assert pm.pano_from_tags(None, None, None) is None


def test_enricher_and_route(client, host):
    d = client.get("/api/panorama/settings").get_json()
    assert d["success"] and d["aspect_min"] == 2.0 and d["auto_detect"] is True
    fn = next(e["fn"] for e in host.file_enrichers if e["module_id"] == "panorama")
    assert fn(host.db(), []) == {}
    assert fn(host.db(), ["nope/none.jpg"]) == {}
    # a row with GPano in the metadata index, and a wide one without, both come back as pano
    db = host.db()
    db.execute("INSERT OR REPLACE INTO files(rel_path, mtime, width, height) VALUES(?,?,?,?)",
               ("_pano_test/sphere.jpg", 0, 1000, 800))
    db.execute("INSERT OR REPLACE INTO files(rel_path, mtime, width, height) VALUES(?,?,?,?)",
               ("_pano_test/wide.jpg", 0, 4000, 2000))
    db.execute("INSERT OR REPLACE INTO files(rel_path, mtime, width, height) VALUES(?,?,?,?)",
               ("_pano_test/plain.jpg", 0, 1000, 800))
    db.execute("INSERT OR REPLACE INTO metadata_index(rel_path, ns, tag, value) VALUES(?,?,?,?)",
               ("_pano_test/sphere.jpg", "xmp", "GPano:ProjectionType", "equirectangular"))
    db.commit()
    try:
        out = fn(db, ["_pano_test/sphere.jpg", "_pano_test/wide.jpg", "_pano_test/plain.jpg"])
        assert out["_pano_test/sphere.jpg"]["pano"]["source"] == "gpano"
        assert out["_pano_test/wide.jpg"]["pano"]["source"] == "aspect"
        assert "_pano_test/plain.jpg" not in out
    finally:
        db.execute("DELETE FROM files WHERE rel_path LIKE '_pano_test/%'")
        db.execute("DELETE FROM metadata_index WHERE rel_path LIKE '_pano_test/%'")
        db.commit()
