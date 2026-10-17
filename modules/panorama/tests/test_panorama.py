"""! @file
@brief panorama: GPano / aspect detection, the file enricher and the settings route.
    ./run_tests.sh modules/panorama
"""
import os

import pytest

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


def test_size_is_not_evidence():
    """A wide picture is not a panorama; the opt-in guess takes exactly 2:1 only."""
    assert pm.pano_from_tags({}, 4000, 2000) is None                      # off by default
    assert pm.pano_from_tags({}, 4000, 2000, auto=True)["source"] == "aspect"
    assert pm.pano_from_tags({}, 4000, 2020, auto=True)["source"] == "aspect"   # within 2 %
    assert pm.pano_from_tags({}, 4000, 1600, auto=True) is None           # 2.5:1: wide, not a sphere
    assert pm.pano_from_tags({}, 6000, 2000, auto=True) is None
    assert pm.pano_from_tags(None, None, None) is None


def test_spherical_video_metadata():
    assert pm.pano_from_spherical(None) is None
    assert pm.pano_from_spherical({"projection": ""}) is None
    assert pm.pano_from_spherical({"projection": "cubemap"}) is None
    p = pm.pano_from_spherical({"projection": "equirectangular", "yaw": 45, "pitch": -5})
    assert p["source"] == "spherical" and p["heading"] == 45.0 and p["pitch"] == -5.0


def _spherical_mp4(path):
    """A tiny MP4 with a Spherical Video V1 uuid box in its trak (what spatialmedia injects)."""
    import shutil, struct, subprocess, uuid
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg not installed")
    plain = path + ".plain.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=64x32:d=0.2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", plain], check=True)
    xml = (b'<?xml version="1.0"?><rdf:SphericalVideo xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
           b'xmlns:GSpherical="http://ns.google.com/videos/1.0/spherical/"><GSpherical:Spherical>true</GSpherical:Spherical>'
           b'<GSpherical:Stitched>true</GSpherical:Stitched><GSpherical:StitchingSoftware>test</GSpherical:StitchingSoftware>'
           b'<GSpherical:ProjectionType>equirectangular'
           b'</GSpherical:ProjectionType></rdf:SphericalVideo>')
    ub = struct.pack(">I4s", 24 + len(xml), b"uuid") + uuid.UUID("ffcc8263-f855-4a93-8814-587a02521fdd").bytes + xml
    d = bytearray(open(plain, "rb").read())

    def boxes(off, end):
        while off + 8 <= end:
            size, typ = struct.unpack(">I4s", d[off:off + 8])
            yield off, (size or end - off), typ
            off += size or end - off
    for moff, msize, mtyp in list(boxes(0, len(d))):
        if mtyp == b"moov":
            for toff, tsize, ttyp in list(boxes(moff + 8, moff + msize)):
                if ttyp == b"trak":
                    d[toff + tsize:toff + tsize] = ub
                    d[toff:toff + 4] = struct.pack(">I", tsize + len(ub))
                    d[moff:moff + 4] = struct.pack(">I", msize + len(ub))
                    open(path, "wb").write(d)
                    return plain
    pytest.fail("no trak box")


def test_video_spherical_probe(tmp_path):
    from media_types import video_spherical
    p = str(tmp_path / "sph.mp4")
    plain = _spherical_mp4(p)
    assert video_spherical(plain) is None
    s = video_spherical(p)
    assert s and s["projection"] == "equirectangular"


def test_enricher_and_route(client, host, tmp_path):
    d = client.get("/api/panorama/settings").get_json()
    assert d["success"] and d["auto_detect"] is False
    fn = next(e["fn"] for e in host.file_enrichers if e["module_id"] == "panorama")
    assert fn(host.db(), []) == {}
    assert fn(host.db(), ["nope/none.jpg"]) == {}
    # GPano in the metadata index counts; a wide picture without does not; a video
    # with Spherical Video metadata counts (probed on first sight, then kept)
    db = host.db()
    vdir = os.path.join(host.media_dir, "_pano_test")
    os.makedirs(vdir, exist_ok=True)
    plain = _spherical_mp4(os.path.join(vdir, "sph.mp4"))
    os.replace(plain, os.path.join(vdir, "flat.mp4"))
    for rel, w, h, kind in (("_pano_test/sphere.jpg", 1000, 800, "image"), ("_pano_test/wide.jpg", 4000, 2000, "image"),
                            ("_pano_test/plain.jpg", 1000, 800, "image"), ("_pano_test/sph.mp4", 64, 32, "video"),
                            ("_pano_test/flat.mp4", 64, 32, "video")):
        db.execute("INSERT OR REPLACE INTO files(rel_path, mtime, width, height, media_kind) VALUES(?,?,?,?,?)",
                   (rel, 0, w, h, kind))
    db.execute("INSERT OR REPLACE INTO metadata_index(rel_path, ns, tag, value) VALUES(?,?,?,?)",
               ("_pano_test/sphere.jpg", "xmp", "GPano:ProjectionType", "equirectangular"))
    db.commit()
    try:
        rels = ["_pano_test/sphere.jpg", "_pano_test/wide.jpg", "_pano_test/plain.jpg",
                "_pano_test/sph.mp4", "_pano_test/flat.mp4"]
        out = fn(db, rels)
        assert out["_pano_test/sphere.jpg"]["pano"]["source"] == "gpano"
        assert "_pano_test/wide.jpg" not in out and "_pano_test/plain.jpg" not in out
        assert out["_pano_test/sph.mp4"]["pano"]["source"] == "spherical"
        assert "_pano_test/flat.mp4" not in out
        rows = {r["rel_path"]: r["projection"] for r in db.execute("SELECT rel_path, projection FROM pano_video")}
        assert rows["_pano_test/sph.mp4"] == "equirectangular" and rows["_pano_test/flat.mp4"] == ""
        assert fn(db, rels)["_pano_test/sph.mp4"]["pano"]["source"] == "spherical"   # from the table now
    finally:
        db.execute("DELETE FROM files WHERE rel_path LIKE '_pano_test/%'")
        db.execute("DELETE FROM metadata_index WHERE rel_path LIKE '_pano_test/%'")
        db.execute("DELETE FROM pano_video WHERE rel_path LIKE '_pano_test/%'")
        db.commit()
        import shutil
        shutil.rmtree(vdir, ignore_errors=True)