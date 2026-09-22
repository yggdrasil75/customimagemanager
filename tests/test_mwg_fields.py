"""mwg-rs region XML: write → parse round trip, and the type==class rule."""
import json
from xml.sax.saxutils import escape
from modules.metadata import mwg_fields as m

P = "Xmp.mwg-rs.Regions/mwg-rs:RegionList[1]"


def _xmp(**kw):
    x = {f"{P}/mwg-rs:Area/stArea:x": .5, f"{P}/mwg-rs:Area/stArea:y": .5,
         f"{P}/mwg-rs:Area/stArea:w": .4, f"{P}/mwg-rs:Area/stArea:h": .8}
    for k, v in kw.items():
        x[f"{P}/mwg-rs:{k}"] = v
    return x


def _parse(x, cls=""):
    return m.parse_region_list(x, lambda raw: ("", [], cls))


def _build(regions):
    blk, ns = m.build_region_list_xml(regions, escape,
                                      lambda b: json.dumps({"description": "", "tags": []}),
                                      lambda n: "link", lambda: "u1")
    return blk, ns


def test_write_type_is_class_and_name_blank():
    r = [{"class_name": "person", "region_name": "", "cx": .5, "cy": .5, "w": .4, "h": .8,
          "confirmed": False, "region_tags": [], "region_description": ""}]
    blk, ns = _build(r)
    assert "<mwg-rs:Name></mwg-rs:Name>" in blk
    assert "<mwg-rs:Type>person</mwg-rs:Type>" in blk
    assert "<cim:Confirmed>false</cim:Confirmed>" in blk
    assert r[0]["region_type"] == "person" and r[0]["uuid"] == "u1"
    assert "xmlns:mwg-rs" in ns and "xmlns:cim" in ns


def test_write_keeps_explicit_type_and_instance_name():
    r = [{"class_name": "face", "region_name": "jill", "region_type": "Face",
          "cx": .5, "cy": .5, "w": .4, "h": .8}]
    blk, _ = _build(r)
    assert "<mwg-rs:Name>jill</mwg-rs:Name>" in blk and "<mwg-rs:Type>Face</mwg-rs:Type>" in blk


def test_write_empty():
    assert m.build_region_list_xml([], escape, str, str, str) == ("", "")


def test_parse_new_style():
    o = _parse(_xmp(Type="person", Name=""))[0]
    assert (o["class_name"], o["region_name"], o["region_type"]) == ("person", "", "person")
    assert o["confirmed"] is True                       # no cim:Confirmed → confirmed


def test_parse_legacy_class_in_name_is_lifted():
    o = _parse(_xmp(Type="", Name="face"))[0]
    assert (o["class_name"], o["region_name"], o["region_type"]) == ("face", "", "face")


def test_parse_legacy_named_instance_with_json_class():
    o = _parse(_xmp(Type="", Name="jill"), cls="face")[0]
    assert (o["class_name"], o["region_name"], o["region_type"]) == ("face", "jill", "face")


def test_parse_legacy_unconfirmed_type_overload():
    o = _parse(_xmp(Type="unconfirmed", Name="cat"))[0]
    assert o["confirmed"] is False and o["region_type"] == "cat" and o["class_name"] == "cat"


def test_parse_skips_degenerate_and_bad_boxes():
    x = _xmp(Type="a"); x[f"{P}/mwg-rs:Area/stArea:w"] = 0
    assert _parse(x) == []
    x = _xmp(Type="a"); x[f"{P}/mwg-rs:Area/stArea:x"] = "nope"
    assert _parse(x) == []


def test_roundtrip_with_masks_and_barcode():
    r = [{"class_name": "dog", "cx": .3, "cy": .3, "w": .2, "h": .2, "confirmed": True,
          "barcode_value": "123", "barcode_format": "QR", "barcode_binary": True,
          "mask_svg": {"centerline": "M0 0Z", "underscan": "", "overscan": ""}}]
    blk, _ = _build(r)
    assert "<cim:MaskCenterline>M0 0Z</cim:MaskCenterline>" in blk
    assert "MaskUnderscan" not in blk
    assert "<cim:BarCodeBinary>true</cim:BarCodeBinary>" in blk
    # parse what we wrote (extract leaf values back into a flat dict)
    import re
    x = _xmp(Type="dog", Name="")
    for tag in ("BarCodeValue", "BarCodeFormat", "BarCodeBinary", "MaskCenterline", "Confirmed"):
        mt = re.search(rf"<cim:{tag}>(.*?)</cim:{tag}>", blk)
        x[f"{P}/mwg-rs:Extensions/cim:{tag}"] = mt.group(1)
    o = _parse(x)[0]
    assert o["barcode_value"] == "123" and o["barcode_binary"] is True
    assert o["mask_svg"]["centerline"] == "M0 0Z" and o["confirmed"] is True


def test_collections_and_keywords_readers():
    x = {"Xmp.mwg-coll.Collections[1]/mwg-coll:CollectionName": "Trip",
         "Xmp.mwg-coll.Collections[2]/mwg-coll:CollectionName": "Trip",
         "Xmp.mwg-coll.Collections[3]/mwg-coll:CollectionName": "Work"}
    assert m.parse_collections(x) == ["Trip", "Work"]