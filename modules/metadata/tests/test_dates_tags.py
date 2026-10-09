"""! @file
@brief Metadata module: the taken-date editor (EXIF + XMP write, time-zone modes,
date buckets) and hierarchical tags (import, path tags, tag tree, tagpath:,
rename, prune)."""
import os

import pyexiv2
import pytest

from cimtest import media_path

TT = "tagtree_test"
_RDF = ('<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:lr="http://ns.adobe.com/lightroom/1.0/" xmlns:digiKam="http://www.digikam.org/ns/1.0/" '
        'xmlns:mwg-kw="http://www.metadataworkinggroup.com/schemas/keywords/">{body}'
        '</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end="w"?>')


def _bag(prop, items, kind="Bag"):
    return f"<{prop}><rdf:{kind}>" + "".join(f"<rdf:li>{i}</rdf:li>" for i in items) + f"</rdf:{kind}></{prop}>"


def _import_sidecar(host, fn, body):
    """! @brief Replace a file's sidecar as another tool would write it, then re-index."""
    with open(os.path.splitext(media_path(fn))[0] + ".xmp", "w", encoding="utf-8") as fh:
        fh.write(_RDF.format(body=body))
    host.core.index_file(fn, force=True)


def _sidecar(fn):
    with pyexiv2.Image(os.path.splitext(media_path(fn))[0] + ".xmp") as im:
        return im.read_xmp()


def _row(host, fn):
    return host.db().execute("SELECT * FROM files WHERE rel_path=?", (fn,)).fetchone()


def _tags(host, fn):
    import json
    return json.loads(_row(host, fn)["tags"] or "[]")


def _paths(host, fn):
    return sorted(r[0] for r in host.db().execute(
        "SELECT path FROM tag_tree WHERE rel_path=?", (fn,)).fetchall())


def _list(client, q, folder=TT):
    j = client.get("/api/list", query_string={"q": q, "folder": folder, "page": 0}).get_json()
    assert j.get("success", True) is not False, j
    return sorted(f.get("filename") or f.get("rel_path") for f in j["files"])


# -- dates ---------------------------------------------------------------------------
def test_date_write_round_trip(client, host, upload):
    from modules.metadata import exif_import
    fn = upload(seed=961, name="when.png")
    r = client.post("/api/metadata/date", json={"filename": fn, "datetime": "2019-07-04T15:30:00",
                                                "offset": "+02:00"})
    j = r.get_json()
    assert r.status_code == 200 and j["success"], j
    assert j["datetime"] == "2019-07-04T15:30:00" and j["offset"] == "+02:00"
    # the files row follows: d_original (EXIF / exif:DateTimeOriginal), d_actual (photoshop)
    row = _row(host, fn)
    assert row["d_original"] == "2019-07-04" and row["d_actual"] == "2019-07-04"
    assert j["buckets"]["d_original"] == "2019-07-04"
    v = exif_import.read_values(media_path(fn))
    assert v.get("OffsetTimeOriginal") == "+02:00"
    assert str(v.get("DateTimeOriginal")).replace("-", ":").replace("T", " ").startswith("2019:07:04 15:30:00")
    x = _sidecar(fn)
    assert x["Xmp.photoshop.DateCreated"].startswith("2019-07-04T15:30:00")
    assert x["Xmp.exif.DateTimeOriginal"] == "2019-07-04T15:30:00+02:00"
    g = client.get("/api/metadata/date", query_string={"filename": fn}).get_json()
    assert g["datetime"] == "2019-07-04T15:30:00" and g["offset"] == "+02:00", g
    # EXIF undo history keeps working
    hist = client.post("/api/exif/history", json={"filename": fn}).get_json()
    assert any("DateTimeOriginal" in h["field"] for h in hist["history"]), hist
    # a later date replaces an earlier one (the bucket keeps the earliest of its fields)
    j = client.post("/api/metadata/date", json={"filename": fn, "datetime": "2021-01-02T03:04:05",
                                                "offset": None}).get_json()
    assert j["success"] and j["offset"] is None, j
    assert _row(host, fn)["d_original"] == "2021-01-02"
    g = client.get("/api/metadata/date", query_string={"filename": fn}).get_json()
    assert g["datetime"] == "2021-01-02T03:04:05" and g["offset"] is None, g


def test_date_digitized_field_and_service(host, upload):
    fn = upload(seed=962, name="dig.png")
    svc = host.get_service("metadata")
    out = svc.set_date(fn, "2018-05-06T07:08:09", "-05:30",
                       fields=["DateTimeOriginal", "CreateDate", "xmp:CreateDate"])
    assert out["success"], out
    row = _row(host, fn)
    assert row["d_original"] == "2018-05-06" and row["d_digitized"] == "2018-05-06"
    assert host.get_service("metadata.set_date") is svc.set_date


def test_timezone_mode_semantics(client, host, upload):
    fn = upload(seed=963, name="tz.png")
    post = lambda **b: client.post("/api/metadata/date", json={"filename": fn, **b}).get_json()
    assert post(datetime="2020-06-01T12:00:00", offset="+02:00")["success"]
    # keep the instant: 12:00 at +02:00 is 15:00 at +05:00
    j = post(datetime="2020-06-01T12:00:00", offset="+05:00", tz_mode="keep_instant")
    assert j["success"] and (j["datetime"], j["offset"]) == ("2020-06-01T15:00:00", "+05:00"), j
    # keep the local time: only the zone changes
    j = post(datetime="2020-06-01T15:00:00", offset="-03:00", tz_mode="keep_local")
    assert (j["datetime"], j["offset"]) == ("2020-06-01T15:00:00", "-03:00"), j
    # an explicit old zone; crossing midnight moves the date bucket too
    j = post(datetime="2020-06-01T23:30:00", offset="+09:00", tz_mode="keep_instant", from_offset="+00:00")
    assert (j["datetime"], j["offset"]) == ("2020-06-02T08:30:00", "+09:00"), j
    assert _row(host, fn)["d_original"] == "2020-06-02"


def test_date_bad_input_is_400(client, upload):
    fn = upload(seed=964, name="bad.png")
    post = lambda **b: client.post("/api/metadata/date", json={"filename": fn, **b})
    assert post(datetime="2020-13-01T00:00:00", offset=None).status_code == 400
    assert post(datetime="yesterday", offset=None).status_code == 400
    assert post(datetime="2020-01-01T00:00:00", offset="+2").status_code == 400
    assert post(datetime="2020-01-01T00:00:00", offset="+15:00").status_code == 400
    assert post(datetime="2020-01-01T00:00:00", offset=None, fields=["Nope"]).status_code == 400
    assert post(datetime="2020-01-01T00:00:00", offset="+01:00", tz_mode="sideways").status_code == 400
    # keep_instant without a known zone
    assert post(datetime="2020-01-01T00:00:00", offset="+01:00", tz_mode="keep_instant").status_code == 400
    r = client.post("/api/metadata/date", json={"filename": "no/such.jxl", "datetime": "2020-01-01T00:00:00"})
    assert r.status_code == 404


def test_exif_date_fields_validate():
    from modules.metadata import exif_export as ee, exif_fields as ef
    _, dto = ef.field_by_tagname("DateTimeOriginal")
    _, off = ef.field_by_tagname("OffsetTimeOriginal")
    assert ee._coerce(dto, "2020:01:02 03:04:05") == ("2020:01:02 03:04:05", None)
    assert ee._coerce(dto, "2020-01-02")[1]
    assert ee._coerce(off, "+02:00") == ("+02:00", None)
    assert ee._coerce(off, "2h")[1]


# -- hierarchical tags ---------------------------------------------------------------
def test_hierarchy_import_from_sidecar(host, upload):
    fn = upload(seed=971, name="imp.png", folder=TT)
    body = (_bag("dc:subject", ["nc", "sunset"])
            + _bag("lr:hierarchicalSubject", ["places|usa|nc"])
            + _bag("digiKam:TagsList", ["People/Family/Ann"], "Seq")
            + '<mwg-kw:Keywords rdf:parseType="Resource"><mwg-kw:Hierarchy><rdf:Bag>'
              '<rdf:li rdf:parseType="Resource"><mwg-kw:Keyword>Animals</mwg-kw:Keyword>'
              '<mwg-kw:Children><rdf:Bag><rdf:li rdf:parseType="Resource">'
              '<mwg-kw:Keyword>Cat</mwg-kw:Keyword></rdf:li></rdf:Bag></mwg-kw:Children>'
              '</rdf:li></rdf:Bag></mwg-kw:Hierarchy></mwg-kw:Keywords>')
    _import_sidecar(host, fn, body)
    tags = _tags(host, fn)
    assert {"nc", "sunset", "Ann", "Cat"} <= set(tags), tags
    assert not any("|" in t or "/" in t for t in tags), tags
    assert _paths(host, fn) == ["Animals/Cat", "People/Family/Ann", "places/usa/nc", "sunset"]


def test_path_tag_writes_hierarchical_subject(host, upload):
    fn = upload(seed=972, name="path.png", folder=TT)
    res = host.update_file(fn, add={"tags": ["animals/dog/rex", "plain"]})
    assert res["success"], res
    tags = _tags(host, fn)
    assert "rex" in tags and "plain" in tags and not any("/" in t for t in tags), tags
    assert _sidecar(fn)["Xmp.lr.hierarchicalSubject"] == ["animals|dog|rex"]
    assert _paths(host, fn) == ["animals/dog/rex", "plain"]
    # the Lightroom form works too, and the path round-trips a re-index
    host.update_file(fn, add={"tags": ["animals|cat"]})
    host.core.index_file(fn, force=True)
    assert _paths(host, fn) == ["animals/cat", "animals/dog/rex", "plain"]
    # removing the flat leaf drops its path
    host.update_file(fn, remove={"tags": ["rex"]})
    assert _sidecar(fn)["Xmp.lr.hierarchicalSubject"] == ["animals|cat"]
    host.core.index_file(fn, force=True)
    assert "rex" not in _tags(host, fn)
    assert _paths(host, fn) == ["animals/cat", "plain"]


@pytest.fixture
def tree_lib(host, upload):
    a = upload(seed=981, name="a.png", folder=TT)
    b = upload(seed=982, name="b.png", folder=TT)
    c = upload(seed=983, name="c.png", folder=TT)
    host.update_file(a, add={"tags": ["places/usa/nc", "beach"]})
    host.update_file(b, add={"tags": ["places/usa/ny"]})
    host.update_file(c, add={"tags": ["places/france"]})
    return a, b, c


def _node(tree, path):
    for n in tree:
        if n["path"] == path:
            return n
        if path.startswith(n["path"] + "/"):
            return _node(n["children"], path)
    return None


def test_tree_counts_and_scope(client, tree_lib):
    j = client.get("/api/tags/tree", query_string={"folder": TT}).get_json()
    assert j["success"] and j["files"] == 3, j
    t = j["tree"]
    assert _node(t, "places")["count"] == 3
    assert _node(t, "places/usa")["count"] == 2
    assert _node(t, "places/usa/nc")["count"] == 1
    assert _node(t, "beach")["count"] == 1 and _node(t, "beach")["children"] == []
    # scoped by the gallery query
    j = client.get("/api/tags/tree", query_string={"folder": TT, "q": "tagpath:places/usa"}).get_json()
    assert j["files"] == 2 and _node(j["tree"], "places/france") is None


def test_tagpath_search(client, tree_lib):
    a, b, c = tree_lib
    assert _list(client, "tagpath:places/usa") == sorted([a, b])
    assert _list(client, "tagpath:places") == sorted([a, b, c])
    assert _list(client, "tagpath:PLACES/usa/nc") == [a]
    assert _list(client, "tagpath:places/us") == []
    assert _list(client, "-tagpath:places/usa") == [c]


def test_rename_moves_files(client, host, tree_lib):
    a, b, c = tree_lib
    r = client.post("/api/tags/rename", json={"from": "places/usa", "to": "places/america"})
    j = r.get_json()
    assert r.status_code == 200 and j["success"] and j["changed"] == 2, j
    assert _list(client, "tagpath:places/america") == sorted([a, b])
    assert _list(client, "tagpath:places/usa") == []
    assert _sidecar(a)["Xmp.lr.hierarchicalSubject"] == ["places|america|nc"]
    # renaming a leaf renames the flat tag too
    j = client.post("/api/tags/rename", json={"from": "places/america/nc",
                                              "to": "places/america/carolina"}).get_json()
    assert j["success"] and j["changed"] == 1, j
    tags = _tags(host, a)
    assert "carolina" in tags and "nc" not in tags, tags
    # a flat tag moved under a node becomes a path
    j = client.post("/api/tags/rename", json={"from": "beach", "to": "places/coast/beach"}).get_json()
    assert j["success"], j
    assert "places/coast/beach" in _paths(host, a) and "beach" in _tags(host, a)
    assert client.post("/api/tags/rename", json={"from": "", "to": "x"}).status_code == 400
    assert client.post("/api/tags/rename", json={"from": "nothing/here", "to": "x"}).status_code == 404


def test_rename_mwg_paths_move_into_lightroom(client, host, upload):
    fn = upload(seed=973, name="mwg.png", folder=TT)
    _import_sidecar(host, fn,
                    '<mwg-kw:Keywords rdf:parseType="Resource"><mwg-kw:Hierarchy><rdf:Bag>'
                    '<rdf:li rdf:parseType="Resource"><mwg-kw:Keyword>Pets</mwg-kw:Keyword>'
                    '<mwg-kw:Children><rdf:Bag><rdf:li rdf:parseType="Resource">'
                    '<mwg-kw:Keyword>Rex</mwg-kw:Keyword></rdf:li></rdf:Bag></mwg-kw:Children>'
                    '</rdf:li></rdf:Bag></mwg-kw:Hierarchy></mwg-kw:Keywords>')
    assert _paths(host, fn) == ["Pets/Rex"]
    j = client.post("/api/tags/rename", json={"from": "Pets", "to": "Animals"}).get_json()
    assert j["success"], j
    x = _sidecar(fn)
    assert x["Xmp.lr.hierarchicalSubject"] == ["Animals|Rex"]
    assert not any(k.startswith("Xmp.mwg-kw.") for k in x), x
    assert _paths(host, fn) == ["Animals/Rex"]


def test_hierarchy_helpers():
    from modules.metadata import hierarchy as h
    assert h.segments(" a / b|c ") == ["a", "b", "c"]
    assert h.is_path_tag("a/b") and not h.is_path_tag("a/") and not h.is_path_tag("ab")
    assert h.moved("a/b/c", "a/b", "x") == "x/c" and h.moved("a/bc", "a/b", "x") is None
    assert h.under("A/B", "a") and not h.under("ab", "a")
