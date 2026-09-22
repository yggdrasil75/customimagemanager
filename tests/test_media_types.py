"""media_types.py: naming, content sniffing, extension reconciliation."""
import os
import numpy as np
import pytest
import media_types as mt
from cimtest import png_bytes


def test_stored_name():
    assert mt.stored_name("a.png") == "a.jxl"
    assert mt.stored_name("a.JPG") == "a.jxl"
    assert mt.stored_name("clip.mp4") == "clip.mp4"


def test_predicates():
    assert mt.is_video("x.mkv") and not mt.is_video("x.jxl")
    assert mt.is_jxl("x.JXL")
    assert mt.is_library_file("x.jxl") and mt.is_library_file("x.mp4")
    assert not mt.is_library_file("x.png")
    assert mt.kind("x.jxl") == "image" and mt.kind("x.mp4") == "video"
    assert ".png" in mt.UPLOAD_EXTS_now()


def test_ext_matches_aliases():
    assert mt.ext_matches(".jpg", ".jpeg") and mt.ext_matches(".jpeg", ".jpg")
    assert mt.ext_matches(".png", ".png")
    assert not mt.ext_matches(".png", ".jpg")


def test_sniff_and_reconcile(tmp_path):
    import cv2
    p = tmp_path / "lie.jpg"
    p.write_bytes(png_bytes())
    assert mt.sniff_ext(str(p)) == ".png"
    fn, ext, status = mt.reconcile_ext(str(p), "lie.jpg")
    assert status == "corrected" and fn == "lie.png" and ext == ".png"
    q = tmp_path / "ok.png"; q.write_bytes(png_bytes())
    assert mt.reconcile_ext(str(q), "ok.png")[2] == "ok"
    ok, jpg = cv2.imencode(".jpg", np.zeros((8, 8, 3), np.uint8))
    r = tmp_path / "x.jpeg"; r.write_bytes(jpg.tobytes())
    assert mt.reconcile_ext(str(r), "x.jpeg")[2] == "ok"            # alias, not "corrected"
    z = tmp_path / "junk.bin"; z.write_bytes(b"\x00" * 64)
    assert mt.reconcile_ext(str(z), "junk.bin") == ("junk.bin", None, "unknown")


def test_mime_and_related():
    assert mt.mime_for("a.mp4") == "video/mp4"
    assert ".xmp" in mt.related_exts("/m/a.jxl")


def test_jxl_keyframe_indices_monotone():
    idx = mt.jxl_keyframe_indices(100)
    assert idx == sorted(set(idx)) and idx[0] == 0 and idx[-1] <= 99
    assert mt.jxl_keyframe_indices(1) == [0]
