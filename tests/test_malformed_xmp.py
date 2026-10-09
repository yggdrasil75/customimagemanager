"""! @file
@brief Malformed XMP (exiv2's "XMP Toolkit error 201"): sidecars other tools left
with control characters, a bare "&" or Latin-1 bytes are repaired in place with the
original backed up; a media file with a broken embedded packet is read from a
cleaned copy and never modified; a sidecar rewrite never drops what it held."""
import glob, hashlib, os, struct
import pytest

pyexiv2 = pytest.importorskip("pyexiv2")
import media_types as mt

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
    import shutil
    shutil.rmtree(d, ignore_errors=True)
    shutil.rmtree(os.path.join(app.MEDIA_DIR, ".cim", "xmp-backup"), ignore_errors=True)


def _jpeg(path, xmp=None, artist="bob"):
    """! @brief A tiny JPEG with an Artist tag, plus a raw (unchecked) XMP APP1 segment."""
    from PIL import Image
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
        text = mt.clean_xmp_text(raw)
        assert mt.xmp_well_formed(text) and word.replace("&", "&amp;") in text
    # entities and good text are left alone
    good = HEAD + _subject("a &amp; b &#233; &lt;") + TAIL
    assert mt.clean_xmp_text(good) == good


@pytest.mark.parametrize("kind", sorted(BAD))
def test_sidecar_repaired_and_backed_up(app, media, kind):
    import xmp_import
    raw, word = BAD[kind]
    src = _jpeg(os.path.join(media, f"{kind}.jpg"))
    side = os.path.join(media, f"{kind}.xmp")
    open(side, "wb").write(raw)
    vals, source, _xml = xmp_import.resolve_xmp(src)
    assert source == side and vals.get("Xmp.dc.subject") == [word]
    # the sidecar now opens in exiv2 itself; the original bytes are kept
    with pyexiv2.Image(side) as img:
        assert img.read_xmp()["Xmp.dc.subject"] == [word]
    baks = [b for b in _backups(app) if os.path.basename(b).startswith(f"{kind}.xmp.")]
    assert len(baks) == 1 and open(baks[0], "rb").read() == raw
    # EXIF / IPTC readers of the same file work too
    import exif_import
    assert exif_import._read_raw_exif(src)[0].get("Exif.Image.Artist") == "bob"


def test_well_formed_sidecar_untouched(app, media):
    side = os.path.join(media, "ok.xmp")
    open(side, "wb").write(_packet(_subject("fine")))
    before = _sha(side)
    assert mt.repair_xmp_sidecar(side) is None
    with mt.exiv2_image(side) as img:
        assert img.read_xmp()["Xmp.dc.subject"] == ["fine"]
    assert _sha(side) == before and not _backups(app)


def test_embedded_packet_read_from_clean_copy(app, media):
    import xmp_import, exif_import
    raw, word = BAD["amp"]
    src = _jpeg(os.path.join(media, "emb.jpg"), xmp=raw)
    before = _sha(src)
    with pytest.raises(Exception):
        pyexiv2.Image(src)
    assert xmp_import.resolve_xmp(src)[0].get("Xmp.dc.subject") == [word]
    assert exif_import._read_raw_exif(src)[0].get("Exif.Image.Artist") == "bob"
    assert mt.read_gps(src) is None                       # no crash, no GPS
    with mt.exiv2_image(src) as img:
        with pytest.raises(RuntimeError):
            img.modify_exif({"Exif.Image.Artist": "x"})
    assert _sha(src) == before                            # media file never modified


def test_rewrite_keeps_foreign_fields(app, media):
    src = _jpeg(os.path.join(media, "keep.jpg"))
    side = os.path.join(media, "keep.xmp")
    open(side, "wb").write(_packet("<xmp:Label>red & blue</xmp:Label>\n" + _subject("x\x02y")))
    assert app.write_metadata(src, ["newtag"], "", [])
    with pyexiv2.Image(side) as img:
        xmp = img.read_xmp()
    assert xmp.get("Xmp.xmp.Label") == "red & blue"
    assert xmp.get("Xmp.dc.subject") == ["newtag"]
    assert len(_backups(app)) == 1


def test_unrepairable_sidecar_backed_up_before_rewrite(app, media):
    import xmp_import
    src = _jpeg(os.path.join(media, "broken.jpg"))
    side = os.path.join(media, "broken.xmp")
    raw = _packet("<zz:Thing>kept only in the backup</zz:Thing>\n")  # unbound prefix
    open(side, "wb").write(raw)
    assert xmp_import.resolve_xmp(src)[0] == {}           # unreadable, but no exception
    assert not _backups(app)                              # a read alone doesn't copy
    assert app.write_metadata(src, ["t"], "", [])
    baks = _backups(app)
    assert len(baks) == 1 and open(baks[0], "rb").read() == raw
    with pyexiv2.Image(side) as img:
        assert img.read_xmp().get("Xmp.dc.subject") == ["t"]
