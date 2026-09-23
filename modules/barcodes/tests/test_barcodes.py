"""Barcodes module: /api/barcodes decodes the fixture codes into regions."""
import pytest
from cimtest import expected, read_meta, write_meta


def _decodable(name):
    """Can the raw decoder read this fixture at all? If not, the fixture is too
    hard (code too small in the frame, blurred, angled) and a module failure
    would be about the photo, not the module."""
    try:
        import cv2, zxingcpp
    except ImportError:
        return True
    from cimtest import fixture
    img = cv2.imread(fixture(name), cv2.IMREAD_COLOR)
    try:
        return bool(zxingcpp.read_barcodes(img))
    except Exception:
        return True


@pytest.mark.parametrize("name", ["barcode_qr.png", "barcode_1d.png"])
def test_decode_fixture(client, upload, name):
    import cv2
    from cimtest import fixture as fixture_path
    if not _decodable(name):
        h, w = cv2.imread(fixture_path(name)).shape[:2]
        pytest.skip(f"{name} ({w}x{h}): the raw zxing decoder can't read it either — "
                    f"crop the fixture closer to the code")
    fn = upload.media(name)
    j = client.post("/api/barcodes", json={"filename": fn}).get_json()
    assert j["success"], j
    assert j["regions"], f"{name}: nothing found ({j.get('note')})"
    r = j["regions"][0]
    assert r["class_name"] == "barcode"
    assert r["region_type"] == "BarCode", "MWG standard Type for codes"
    assert r["confirmed"] is False
    want = expected(name)
    if want:
        assert any(x["barcode_value"] == want for x in j["regions"]), [x["barcode_value"] for x in j["regions"]]
        assert want in j["summary"]
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
