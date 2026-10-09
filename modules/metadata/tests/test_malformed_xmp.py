"""! @file
@brief Malformed XMP (exiv2's "XMP Toolkit error 201"): control characters, a bare
"&" or Latin-1 bytes in a sidecar or embedded packet. Reading goes through a
cleaned copy and never modifies the file; before a write the sidecar is repaired
in place with the original kept under .cim/xmp-backup/, so nothing it held is lost."""
import glob
import hashlib
import os
import shutil
import struct
from datetime import datetime, timedelta, timezone

import pytest

pyexiv2 = pytest.importorskip("pyexiv2")
from PIL import Image

import exif_fields
import exif_import
import xmp_export
import xmp_fields
import xmp_import

HEAD = ('<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        '<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:xmp="http://ns.adobe.com/xap/1.0/">\n')
TAIL = '</rdf:Description></rdf:RDF></x:xmpmeta>\n<?xpacket end="w"?>'


def _packet(body):
    return (HEAD + body + TAIL).encode("utf-8")


def _subject(word):
    return f"<dc:subject><rdf:Bag><rdf:li>{word}</rdf:li></rdf:Bag></dc:subject>\n"


BAD = {
    "amp": (_packet(_subject("tom & jerry")), "tom & jerry"),
    "ctrl": (_packet(_subject("cat\x01dog")), "catdog"),
    "latin1": (_packet(_subject("@@")).replace(b"@@", "café".encode("latin-1")), "café"),
}


def _sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def _backups(app):
    return [p for p in glob.glob(os.path.join(app.MEDIA_DIR, ".cim", "xmp-backup", "**", "*"), recursive=True)
            if os.path.isfile(p)]


@pytest.fixture
def media(app):
    """! @brief A scratch folder in the media dir (with its .cim), removed after."""
    d = os.path.join(app.MEDIA_DIR, "xmp_bad")
    os.makedirs(d, exist_ok=True)
    os.makedirs(os.path.join(app.MEDIA_DIR, ".cim"), exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)
    shutil.rmtree(os.path.join(app.MEDIA_DIR, ".cim", "xmp-backup"), ignore_errors=True)


def _jpeg(path, xmp=None, artist="bob"):
    """! @brief A tiny JPEG with an Artist tag, plus a raw (unchecked) XMP APP1 segment."""
    Image.new("RGB", (8, 8)).save(path)
    with pyexiv2.Image(path) as img:
        img.modify_exif({"Exif.Image.Artist": artist})
    if xmp is not None:
        data = open(path, "rb").read()
        seg = b"http://ns.adobe.com/xap/1.0/\x00" + xmp
        app1 = b"\xff\xe1" + struct.pack(">H", len(seg) + 2) + seg
        open(path, "wb").write(data[:2] + app1 + data[2:])
    return path


def test_clean_xmp_text():
    for raw, word in BAD.values():
        with pytest.raises(Exception):
            pyexiv2.ImageData(raw)
        text = xmp_import.clean_xmp_text(raw)
        assert xmp_import.xmp_well_formed(text) and word.replace("&", "&amp;") in text
    # entities and good text are left alone
    good = HEAD + _subject("a &amp; b &#233; &lt;") + TAIL
    assert xmp_import.clean_xmp_text(good) == good


@pytest.mark.parametrize("kind", sorted(BAD))
def test_sidecar_read_without_touching_it(app, media, kind):
    raw, word = BAD[kind]
    src = _jpeg(os.path.join(media, f"{kind}.jpg"))
    side = os.path.join(media, f"{kind}.xmp")
    open(side, "wb").write(raw)
    vals, source, _xml = xmp_import.resolve_xmp(src)
    assert source == side and vals.get("Xmp.dc.subject") == [word]
    assert exif_import._read_raw_exif(src)[0].get("Exif.Image.Artist") == "bob"
    assert open(side, "rb").read() == raw and not _backups(app)


@pytest.mark.parametrize("kind", sorted(BAD))
def test_write_repairs_sidecar_and_keeps_original(app, media, kind):
    raw, word = BAD[kind]
    src = _jpeg(os.path.join(media, f"w{kind}.jpg"))
    side = os.path.join(media, f"w{kind}.xmp")
    open(side, "wb").write(raw)
    assert app.update_file(src, add={"tags": ["added"]})["success"]
    with pyexiv2.Image(side) as img:                      # exiv2 itself opens it now
        assert sorted(img.read_xmp()["Xmp.dc.subject"]) == sorted([word, "added"])
    baks = _backups(app)
    assert len(baks) == 1 and open(baks[0], "rb").read() == raw


def test_well_formed_sidecar_untouched(app, media):
    side = os.path.join(media, "ok.xmp")
    open(side, "wb").write(_packet(_subject("fine")))
    before = _sha(side)
    assert xmp_export.repair_sidecar(side) is None
    with xmp_import.open_image(side) as img:
        assert img.read_xmp()["Xmp.dc.subject"] == ["fine"]
    assert _sha(side) == before and not _backups(app)


def test_embedded_packet_read_from_clean_copy(app, host, media):
    raw, word = BAD["amp"]
    src = _jpeg(os.path.join(media, "emb.jpg"), xmp=raw)
    before = _sha(src)
    with pytest.raises(Exception):
        pyexiv2.Image(src)
    assert xmp_import.resolve_xmp(src)[0].get("Xmp.dc.subject") == [word]
    assert exif_import._read_raw_exif(src)[0].get("Exif.Image.Artist") == "bob"
    assert host.get_service("xmp")["read_gps"](src) is None   # no crash, no GPS
    with host.get_service("exiv2")["open"](src) as img:
        assert img.read_exif().get("Exif.Image.Artist") == "bob"
        with pytest.raises(RuntimeError):
            img.modify_exif({"Exif.Image.Artist": "x"})
    assert _sha(src) == before                            # media file never modified


def test_rewrite_keeps_foreign_fields(app, media):
    src = _jpeg(os.path.join(media, "keep.jpg"))
    side = os.path.join(media, "keep.xmp")
    open(side, "wb").write(_packet("<xmp:Label>red & blue</xmp:Label>\n" + _subject("x\x02y")))
    assert app.update_file(src, set={"tags": ["newtag"]})["success"]
    with pyexiv2.Image(side) as img:
        xmp = img.read_xmp()
    assert xmp.get("Xmp.xmp.Label") == "red & blue"
    assert xmp.get("Xmp.dc.subject") == ["newtag"]
    assert len(_backups(app)) == 1


def test_unrepairable_sidecar_backed_up_before_rewrite(app, media):
    src = _jpeg(os.path.join(media, "broken.jpg"))
    side = os.path.join(media, "broken.xmp")
    raw = _packet("<zz:Thing>kept only in the backup</zz:Thing>\n")  # unbound prefix
    open(side, "wb").write(raw)
    assert xmp_import.resolve_xmp(src)[0] == {}           # unreadable, but no exception
    assert not _backups(app)                              # a read alone doesn't copy
    assert app.update_file(src, set={"tags": ["t"]})["success"]
    baks = _backups(app)
    assert len(baks) == 1 and open(baks[0], "rb").read() == raw
    with pyexiv2.Image(side) as img:
        assert img.read_xmp().get("Xmp.dc.subject") == ["t"]


def test_gps_and_date_values():
    toks = xmp_fields.gps_xmp(35.5, -78.25)
    assert xmp_fields.parse_xmp_gps(toks["exif:GPSLatitude"]) == pytest.approx(35.5)
    assert xmp_fields.parse_xmp_gps(toks["exif:GPSLongitude"]) == pytest.approx(-78.25)
    assert xmp_fields.gps_xmp(0, 0) == {} and xmp_fields.gps_xmp(91, 0) == {}
    assert exif_fields.exif_degrees("35/1 30/1 0/1", "N") == pytest.approx(35.5)
    assert exif_fields.exif_degrees("78/1 15/1 0/1", "W") == pytest.approx(-78.25)
    assert exif_fields.exif_degrees(None) is None
    assert xmp_fields.xmp_date(datetime(2020, 1, 2, 3, 4, 5)) == "2020-01-02T03:04:05Z"
    tz = timezone(timedelta(hours=-5))
    assert xmp_fields.xmp_date(datetime(2020, 1, 2, 3, 4, 5, tzinfo=tz)) == "2020-01-02T03:04:05-05:00"
