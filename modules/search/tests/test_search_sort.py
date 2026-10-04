"""search_sort: filter tokens and sort: keys against synthetic files rows."""
import json
import pytest
from common import table_exists

ROOT = "zz_search_sort_test"
ROWS = [  # name, w, h, tags
    ("a.png", 1920, 1080, ["female", "outdoor"]),
    ("b.jpg", 1080, 1920, ["male", "outdoor"]),
    ("c.png", 1000, 1000, ["female", "male"]),
    ("d.png", 3000, 1000, ["?female", "hair_long"]),
]


@pytest.fixture
def rows(app):
    db = app._db()
    for n, w, h, t in ROWS:
        db.execute("INSERT OR REPLACE INTO files(rel_path,width,height,tags) VALUES(?,?,?,?)",
                   (f"{ROOT}/{n}", w, h, json.dumps(t)))
    db.commit()
    yield
    db.execute("DELETE FROM files WHERE rel_path LIKE ?", (ROOT + "/%",))
    db.commit()


def q(app, search):
    entries, _ = app._query_files(search, 0, 100, folder=ROOT)
    return [e["filename"].rsplit("/", 1)[1] for e in entries if e.get("kind") == "image"]


@pytest.mark.parametrize("search,want", [
    ("tags:+female,-male", ["a.png", "d.png"]),
    ("tags:female tags:-male", ["a.png", "d.png"]),
    ("-tags:male", ["a.png", "d.png"]),
    ("tags:hair*", ["d.png"]),
    ("tags:hair_*", ["d.png"]),
    ("tags:male|hair_long", ["b.jpg", "c.png", "d.png"]),
    ("tagcount:>2", []),
    ("ratio:16:9", ["a.png"]),
    ("ratio:1.6", []),
    ("ratio:1.6~0.15", ["a.png"]),
    ("ratio:9/16", ["b.jpg"]),
    ("ratio:square", ["c.png"]),
    ("ratio:portrait|ultrawide", ["b.jpg", "d.png"]),
    ("-ratio:landscape", ["b.jpg", "c.png"]),
    ("ratio:>1.5", ["a.png", "d.png"]),
    ("ratio:1..2", ["a.png", "c.png"]),
    ("orient:portrait", ["b.jpg"]),
    ("mp:>2.5", ["d.png"]),
    ("pixels:1000000", ["c.png"]),
    ("ext:jpg", ["b.jpg"]),
    ("ext:png|jpg", ["a.png", "b.jpg", "c.png", "d.png"]),
    ("path:c.p", ["c.png"]),
])
def test_filters(app, rows, search, want):
    assert q(app, search) == want


@pytest.mark.parametrize("search,want", [
    ("sort:width", ["c.png", "b.jpg", "a.png", "d.png"]),
    ("sort:-height", ["b.jpg", "a.png", "c.png", "d.png"]),
    ("sort:height:desc sort:-width", ["b.jpg", "a.png", "d.png", "c.png"]),
    ("sort:ratio", ["b.jpg", "c.png", "a.png", "d.png"]),
    ("sort:-pixels", ["d.png", "a.png", "b.jpg", "c.png"]),
    ("sort:bogus", ["a.png", "b.jpg", "c.png", "d.png"]),
    ("tags:female sort:-width", ["d.png", "a.png", "c.png"]),
])
def test_sort(app, rows, search, want):
    assert q(app, search) == want


def test_name_and_people(app, rows):
    db = app._db()
    if not table_exists(db, "face_regions"):
        pytest.skip("people module off")
    db.execute("INSERT INTO face_regions(rel_path,cx,cy,w,h,name) VALUES(?,.5,.5,.1,.1,'Alice')",
               (f"{ROOT}/b.jpg",))
    db.execute("INSERT INTO face_regions(rel_path,cx,cy,w,h,name) VALUES(?,.2,.2,.1,.1,'Bob')",
               (f"{ROOT}/b.jpg",))
    db.execute("INSERT INTO face_regions(rel_path,cx,cy,w,h,name) VALUES(?,.5,.5,.1,.1,'Carol')",
               (f"{ROOT}/c.png",))
    db.commit()
    try:
        assert q(app, "name:alice") == ["b.jpg"]
        assert q(app, "name:alice,bob") == ["b.jpg"]
        assert q(app, "name:alice|carol") == ["b.jpg", "c.png"]
        assert q(app, "name:car*") == ["c.png"]
        assert q(app, "-name:alice") == ["a.png", "c.png", "d.png"]
        assert q(app, "people:>=2") == ["b.jpg"]
        assert q(app, "sort:name") == ["b.jpg", "c.png", "a.png", "d.png"]
        assert q(app, "sort:-name")[:2] == ["c.png", "b.jpg"]
        assert q(app, "sort:-people")[0] == "b.jpg"
    finally:
        db.execute("DELETE FROM face_regions WHERE rel_path LIKE ?", (ROOT + "/%",))
        db.commit()


def test_sort_doesnt_hide_providers(app):
    seen = []
    app.module_host.search_providers.append(lambda t, f, s: seen.append(s) or [])
    try:
        app._query_files("sort:width", 0, 1)
    finally:
        app.module_host.search_providers.pop()
    assert seen == [[]]