"""Barcodes module: /api/barcodes decodes the fixture codes into regions."""
import os

import pytest
from cimtest import expected, read_meta, write_meta


def _zxing(img):
    """What the bare decoder reads in an image: a list of payloads, or None
    when zxing-cpp isn't installed (the app then falls back to OpenCV)."""
    try:
        import zxingcpp
    except ImportError:
        return None
    try:
        return [r.text for r in zxingcpp.read_barcodes(img)]
    except Exception as e:
        return [f"error: {type(e).__name__}: {e}"]


def _diagnose(app, name, stored):
    """Where a code gets lost: in the file, in the JXL conversion, or in the
    module's own scan. Each step is reported so the failure names the step."""
    import cv2
    from cimtest import fixture as fixture_path, media_path
    from modules.barcodes import scan

    lines = []
    raw = cv2.imread(fixture_path(name), cv2.IMREAD_COLOR)
    h, w = raw.shape[:2]
    lines.append(f"fixture file: {w}x{h}, bare decoder reads {_zxing(raw)}")
    img = app._to_bgr(app.read_jxl(media_path(stored)))
    if img is None:
        lines.append("stored .jxl: could not be decoded at all")
    else:
        sh, sw = img.shape[:2]
        lines.append(f"stored .jxl: {sw}x{sh}, bare decoder reads {_zxing(img)}")
        r = scan.scan(img, None, deep=True) or {}
        lines.append(f"scan.scan(deep=True) on the stored image: "
                     f"{len(r.get('codes') or [])} codes via {r.get('engine')} ({r.get('note')})")
    return "\n  ".join(lines)


@pytest.mark.parametrize("name", ["barcode_qr.png", "barcode_1d.png"])
def test_decode_fixture(client, upload, app, name):
    import cv2
    from cimtest import fixture as fixture_path
    if _zxing(cv2.imread(fixture_path(name), cv2.IMREAD_COLOR)) == []:
        h, w = cv2.imread(fixture_path(name)).shape[:2]
        pytest.skip(f"{name} ({w}x{h}): the bare zxing decoder can't read this file either, "
                    f"so it's the fixture — crop it closer to the code")
    fn = upload.media(name)
    j = client.post("/api/barcodes", json={"filename": fn}).get_json()
    assert j["success"], j
    assert j["regions"], (f"{name}: /api/barcodes found nothing ({j.get('note')})\n  "
                          + _diagnose(app, name, fn))
    r = j["regions"][0]
    assert r["class_name"] == "barcode"
    assert r["region_type"] == "BarCode", "MWG standard Type for codes"
    assert r["confirmed"] is False
    want = expected(name)
    if want:
        got = [x["barcode_value"] for x in j["regions"]]
        # Case is not part of the payload for the alphanumeric symbologies
        # (Code 39 is upper-case only, and hand scanners upper-case what they
        # read), so compare case-insensitively.
        if want.casefold() not in [g.casefold() for g in got]:
            import cv2
            from cimtest import fixture as fixture_path
            bare = _zxing(cv2.imread(fixture_path(name), cv2.IMREAD_COLOR)) or []
            if {g.casefold() for g in got} & {q.casefold() for q in bare}:
                pytest.fail(f"the code in {name} reads {got}, but {os.path.splitext(name)[0]}.txt "
                            f"says {want!r} — the module and the bare decoder agree, so the "
                            f"expectation file is stale; update it.")
            pytest.fail(f"{name}: decoded {got}, expected {want!r} (bare decoder reads {bare})")
        assert want.casefold() in j["summary"].casefold()
    # a decoded code round-trips through the sidecar
    write_meta(client, fn, regions=j["regions"])
    back = read_meta(client, fn)["regions"]
    assert back[0]["barcode_value"] == r["barcode_value"]
    assert back[0]["barcode_format"] == r["barcode_format"]


def test_nothing_on_plain_photo(client, upload):
    fn = upload.media("no_person.jpg")
    assert client.post("/api/barcodes", json={"filename": fn}).get_json()["regions"] == []


def test_missing_file(client):
    assert client.post("/api/barcodes", json={"filename": "nope.jxl"}).get_json()["success"] is False
