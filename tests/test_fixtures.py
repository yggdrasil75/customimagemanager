"""Are the fixture files what the tests assume?

When a model test fails on tests/fixtures/person_single.jpg, there are two
suspects: the model and the photo. These tests check the photo, so a whole
column of red in test_providers.py has one obvious explanation. They use the
PICKED models (Models tab) only — a failure here means "this fixture doesn't
work with the models this machine is set up with", which is exactly the
premise the other tests rely on.
"""
import numpy as np
import pytest
from cimtest import (fixture, find_fixture, expected, has_fixture, load_image,
                     picked_model, text_matches)

IMAGES = ["person_single.jpg", "person_multi.jpg", "face_closeup.jpg", "no_person.jpg",
          "same_person_a.jpg", "same_person_b.jpg", "other_person.jpg",
          "near_dup_a.jpg", "near_dup_b.jpg", "text_document.jpg",
          "barcode_qr.png", "barcode_1d.png", "photo_exif.jpg", "photo_with_xmp.jpg"]


def test_which_fixtures_are_present(record_property):
    """Not a check — a record. Shows what the run had to work with."""
    have = [n for n in IMAGES + ["animated.gif", "clip.mp4", "book.epub", "comic.cbz",
                                 "song.mp3", "photo_with_xmp.xmp"] if has_fixture(n)]
    record_property("fixtures", have)
    assert have, "no fixture media at all — see tests/fixtures/README.md"


@pytest.mark.parametrize("name", IMAGES)
def test_image_is_usable(name):
    img = load_image(name)
    h, w = img.shape[:2]
    assert min(h, w) >= 64, f"{find_fixture(name)} is {w}x{h}: too small to detect anything in"
    assert max(h, w) <= 6000, f"{find_fixture(name)} is {w}x{h}: shrink it, model tests will crawl"
    assert float(img.std()) > 2, f"{find_fixture(name)} is nearly blank"


def test_person_single_holds_exactly_one_person(app):
    """The premise of most model tests: the picked person detector sees ONE
    person here. If this fails, every 'no person / no skeleton / no mask on
    person_single' failure elsewhere is this file, not those models — use a
    plain photo of one whole standing person, the kind the detector was
    trained on (a real photo, not art or a render, unless your picked model
    is trained for that)."""
    run = picked_model(app, "detect.persons")
    boxes = run(load_image("person_single.jpg")) or []
    strong = [b for b in boxes if float(b.get("conf", 1)) >= 0.5]
    assert strong, "the picked detect.persons model finds no person in person_single.jpg"
    assert len(strong) == 1, f"{len(strong)} people in person_single.jpg: {[b.get('conf') for b in strong]}"
    assert strong[0]["h"] > 0.4, (f"the person fills only {strong[0]['h']:.0%} of the frame height; "
                                  f"tests expect a full-body subject")


def test_person_multi_holds_several_people(app):
    run = picked_model(app, "detect.persons")
    boxes = [b for b in (run(load_image("person_multi.jpg")) or []) if float(b.get("conf", 1)) >= 0.5]
    assert len(boxes) >= 2, f"person_multi.jpg: the picked detector sees {len(boxes)} people, want >= 2"


def test_no_person_really_has_none(app):
    run = picked_model(app, "detect.persons")
    boxes = [b for b in (run(load_image("no_person.jpg")) or []) if float(b.get("conf", 1)) >= 0.5]
    assert not boxes, f"no_person.jpg has {len(boxes)} people in it — pick a photo with nobody in it"


def test_face_fixtures_have_faces(app):
    run = picked_model(app, "detect.faces")
    for name, want in (("face_closeup.jpg", 1), ("same_person_a.jpg", 1),
                       ("same_person_b.jpg", 1), ("other_person.jpg", 1)):
        faces = run(load_image(name)) or []
        assert len(faces) >= want, f"{name}: the picked detector finds {len(faces)} faces"
    assert not (run(load_image("no_person.jpg")) or []), "no_person.jpg has a face in it"


def test_near_dup_pair_really_is_a_pair():
    """Same picture, different size/quality — not two different photos."""
    import cv2
    a, b = load_image("near_dup_a.jpg"), load_image("near_dup_b.jpg")
    ar, br = (a.shape[1] / a.shape[0]), (b.shape[1] / b.shape[0])
    assert abs(ar - br) < 0.1, f"near_dup_a/b have different aspect ratios ({ar:.2f} vs {br:.2f})"
    small = [cv2.cvtColor(cv2.resize(i, (64, 64)), cv2.COLOR_BGR2GRAY).astype(np.float32) for i in (a, b)]
    diff = float(np.abs(small[0] - small[1]).mean())
    assert diff < 25, f"near_dup_a/b look like different photos (mean pixel difference {diff:.0f}/255)"


def test_expectation_files_are_sane():
    """barcode_*.txt must be the exact payload; text_document.txt is matched
    loosely (word recall), so a whole page is fine but a short phrase from the
    image is a sharper test."""
    for name in ("barcode_qr.png", "barcode_1d.png"):
        if has_fixture(name):
            want = expected(name)
            assert want, f"{name} has no {name.rsplit('.', 1)[0]}.txt with its decoded value"
            assert "\n" not in want and len(want.split()) <= 12, \
                f"{name}: the .txt must be the exact decoded payload, one line"
    if has_fixture("text_document.jpg"):
        want = expected("text_document.jpg") or ""
        assert want, "text_document.jpg has no text_document.txt saying what must be read"
        ok, detail = text_matches(want, want)
        assert ok, detail
