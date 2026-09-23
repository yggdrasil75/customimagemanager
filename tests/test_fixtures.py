"""Are the fixture files what the tests assume?

When a model test fails on tests/fixtures/person_single.jpg, there are two
suspects: the model and the photo. These tests check the photo, so a whole
column of red in test_providers.py has one obvious explanation. They use the
PICKED models (Models tab) only — a failure here means "this fixture doesn't
work with the models this machine is set up with", which is exactly the
premise the other tests rely on.
"""
import warnings

import numpy as np
import pytest
import cimtest
from cimtest import (fixture, find_fixture, expected, free_models, has_fixture,
                     load_image, picked_model, text_matches)

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
    assert float(img.std()) > 2, f"{find_fixture(name)} is nearly blank"
    if max(h, w) > 6000:
        # Not a failure: models resize internally. It does cost decode time on
        # every model test, and a subject that's small in a 40 MP frame is
        # harder for detectors than the same subject cropped.
        warnings.warn(f"{find_fixture(name)} is {w}x{h}; a ~2000px copy would run faster "
                      f"and detect just as well")


def _count_people(app, name):
    """People found in a fixture by EVERY installed person detector, one at a
    time (freeing each before the next). Two suspects, one answer: if they all
    see nothing the photo is wrong; if only your picked model sees nothing,
    the pick is wrong for this kind of image."""
    img = load_image(name)
    b = app.module_host.broker
    counts = {}
    for pid, p in (b._providers.get("detect.persons") or {}).items():
        if pid == cimtest.FAKE_ID or p.resource or not p.available():
            continue
        try:
            boxes = b.request("detect.persons", provider=pid)(img) or []
            counts[pid] = len([x for x in boxes if float(x.get("conf", 1)) >= 0.5])
        except Exception as e:
            counts[pid] = f"error: {type(e).__name__}"
        free_models()
    if not counts:
        pytest.skip("no installed detect.persons model to check the fixture with")
    return counts, b.selected_id("detect.persons")


def _verdict(name, counts, picked, want, got):
    ok = [f"{k}={v}" for k, v in counts.items() if v == want]
    return (f"{name}: your picked detector '{picked}' finds {got}, want {want}.\n"
            f"  all installed detectors: {counts}\n"
            + (f"  these agree with the fixture: {ok} — so the PICK is wrong for this kind of "
               f"image (an anime/illustration model won't see people in photos, and vice "
               f"versa); change it in Settings or pick a fixture that matches your library."
               if ok else
               "  no detector sees it that way, so the PHOTO is the problem: use a plain "
               "shot of the subject your models are trained for."))


def test_person_single_holds_exactly_one_person(app):
    """The premise of most model tests. When this fails, every 'no person / no
    skeleton / no mask on person_single' failure elsewhere follows from it —
    top-down pose and segmentation models are fed by this detector."""
    counts, picked = _count_people(app, "person_single.jpg")
    got = counts.get(picked)
    assert got == 1, _verdict("person_single.jpg", counts, picked, 1, got)


def test_person_multi_holds_several_people(app):
    counts, picked = _count_people(app, "person_multi.jpg")
    got = counts.get(picked)
    assert isinstance(got, int) and got >= 2, _verdict("person_multi.jpg", counts, picked, ">=2", got)


def test_no_person_really_has_none(app):
    counts, picked = _count_people(app, "no_person.jpg")
    assert counts.get(picked) == 0, _verdict("no_person.jpg", counts, picked, 0, counts.get(picked))


def test_face_fixtures_have_faces(app):
    run = picked_model(app, "detect.faces")
    picked = app.module_host.broker.selected_id("detect.faces")
    for name, want in (("face_closeup.jpg", 1), ("same_person_a.jpg", 1),
                       ("same_person_b.jpg", 1), ("other_person.jpg", 1)):
        faces = run(load_image(name)) or []
        assert len(faces) >= want, (f"{name}: the picked face detector '{picked}' finds "
                                    f"{len(faces)} faces, want >= {want}")
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
