"""! @file
@brief Album-level data: description, per-album sort order, manual order (drag to
reorder), the album level check on the edit routes, and the rebuild path (the
cover file's cim:Data "album" entry and members' "album_positions" restore what
a fresh DB lacks on a library.sync pull)."""
import contextlib
import pytest
from flask import g
import features


def _order(client, album, q=""):
    j = client.get("/api/list", query_string={"album": album, "q": q}).get_json()
    assert j["success"], j
    return [f["filename"] for f in j["files"]]


def _albums(client):
    return {a["name"]: a for a in client.get("/api/albums").get_json()["albums"]}


def _post(client, url, **body):
    return client.post(url, json=body)


@pytest.fixture
def three(client, upload, host):
    """! @brief An album "Sorty" with three files, joined in the order c, a, b."""
    a = upload("alb_a.png", seed=301)
    b = upload("alb_b.png", seed=302)
    c = upload("alb_c.png", seed=303)
    assert _post(client, "/api/albums/create", name="Sorty", description="first words",
                 files=[c]).get_json()["success"]
    assert _post(client, "/api/albums/add", album="Sorty", files=[a]).get_json()["added"] == 1
    assert _post(client, "/api/albums/add", album="Sorty", files=[b]).get_json()["added"] == 1
    db = host.db()
    for i, rel in enumerate((c, a, b)):
        db.execute("UPDATE album_members SET added=? WHERE album='Sorty' AND rel_path=?",
                   (1000.0 + i, rel))
    db.commit()
    yield a, b, c
    _post(client, "/api/albums/delete", name="Sorty")


def test_describe_and_create_description(client, three, host):
    a, b, c = three
    assert _albums(client)["Sorty"]["description"] == "first words"
    r = _post(client, "/api/albums/describe", album="Sorty", description="  A long weekend  ")
    assert r.status_code == 200 and r.get_json()["description"] == "A long weekend"
    assert _albums(client)["Sorty"]["description"] == "A long weekend"
    # mirrored into the cover file (the first member by path when none is chosen)
    cover = _albums(client)["Sorty"]["cover"]
    assert cover == min(a, b, c)
    entry = host.core.file_data(cover, "album")
    assert entry["name"] == "Sorty" and entry["description"] == "A long weekend"
    assert _post(client, "/api/albums/describe", album="Nope", description="x").status_code == 404
    assert _post(client, "/api/albums/describe", album="", description="x").status_code == 400


def test_sort_modes(client, three, host):
    a, b, c = three
    assert _order(client, "Sorty") == sorted([a, b, c])               # default: path
    for tok, want in (("added", [c, a, b]), ("-added", [b, a, c]),
                      ("-path", sorted([a, b, c], reverse=True)), ("", sorted([a, b, c]))):
        r = _post(client, "/api/albums/sort", album="Sorty", sort=tok)
        assert r.status_code == 200, r.get_json()
        assert _albums(client)["Sorty"]["sort"] == tok
        assert _order(client, "Sorty") == want, tok
    if "taken" in host.sort_keys:
        assert _post(client, "/api/albums/sort", album="Sorty", sort="-taken").status_code == 200
        assert set(_order(client, "Sorty")) == {a, b, c}
    assert _post(client, "/api/albums/sort", album="Sorty", sort="bogus").status_code == 400
    # the search box's own sort: wins over the album's
    _post(client, "/api/albums/sort", album="Sorty", sort="-added")
    assert _order(client, "Sorty", q="sort:path") == sorted([a, b, c])
    assert _order(client, "Sorty") == [b, a, c]
    # mirrored with the description
    entry = host.core.file_data(_albums(client)["Sorty"]["cover"], "album")
    assert entry["sort"] == "-added"


def test_manual_order(client, three, host):
    a, b, c = three
    r = _post(client, "/api/albums/order", album="Sorty", rel_paths=[b, c, a])
    assert r.status_code == 200 and r.get_json()["sort"] == "manual"
    assert _albums(client)["Sorty"]["sort"] == "manual"
    assert _order(client, "Sorty") == [b, c, a]
    assert host.core.file_data(b, "album_positions") == {"Sorty": 0}
    assert host.core.file_data(a, "album_positions") == {"Sorty": 2}
    # a partial list reorders only within the slots it holds
    assert _post(client, "/api/albums/order", album="Sorty", rel_paths=[a, c]).status_code == 200
    assert _order(client, "Sorty") == [b, a, c]
    # strangers are ignored; nothing valid is a 400
    assert _post(client, "/api/albums/order", album="Sorty", rel_paths=["nope.jxl"]).status_code == 400
    # a re-index keeps the position (and when the file joined)
    assert host.core.index_file(a, force=True) is not False
    assert _order(client, "Sorty") == [b, a, c]
    # a rename carries positions and the mirrored entry over
    assert _post(client, "/api/albums/rename", name="Sorty", new_name="Sorty2").get_json()["success"]
    try:
        assert _order(client, "Sorty2") == [b, a, c]
        assert _albums(client)["Sorty2"]["description"] == "first words"
        assert host.core.file_data(a, "album_positions") == {"Sorty2": 1}
        names = {e["name"] for e in [host.core.file_data(_albums(client)["Sorty2"]["cover"], "album")]}
        assert names == {"Sorty2"}
    finally:
        _post(client, "/api/albums/rename", name="Sorty2", new_name="Sorty")


def test_rebuild_restores_album_data(client, three, host):
    a, b, c = three
    _post(client, "/api/albums/describe", album="Sorty", description="kept in the file")
    _post(client, "/api/albums/order", album="Sorty", rel_paths=[c, b, a])
    db = host.db()
    # a rebuilt DB: album-level values and positions gone
    db.execute("UPDATE albums SET description='', sort='' WHERE name='Sorty'")
    db.execute("UPDATE album_members SET position=NULL WHERE album='Sorty'")
    db.commit()
    assert _albums(client)["Sorty"]["description"] == ""
    host.emit("library.sync", direction="pull", rel_paths=None)
    al = _albums(client)["Sorty"]
    assert al["description"] == "kept in the file" and al["sort"] == "manual"
    assert _order(client, "Sorty") == [c, b, a]
    # the albums row itself gone: rebuilt from the members' sidecars
    db.execute("DELETE FROM albums WHERE name='Sorty'")
    db.commit()
    host.emit("library.sync", direction="pull", rel_paths=None)
    assert _albums(client)["Sorty"]["description"] == "kept in the file"


def test_cover_moves_the_mirror(client, three, host):
    a, b, c = three
    first = min(a, b, c)
    other = max(a, b, c)
    assert host.core.file_data(first, "album")["name"] == "Sorty"
    assert _post(client, "/api/albums/set_cover", album="Sorty", cover=other).status_code == 200
    assert host.core.file_data(first, "album") is None
    entry = host.core.file_data(other, "album")
    assert entry["cover"] is True and entry["description"] == "first words"


@contextlib.contextmanager
def _as(app, acct):
    """! @brief Swap g.user for a real non-admin account for one block (auth is off in
    the test app, so every request would otherwise be the anonymous admin)."""
    def _swap():
        if getattr(g, "user", None) is not None:
            g.user = {**acct, "is_admin": False, "features": features.effective_permissions(
                "custom", {"tab.albums": "write"})}
    app.app.before_request_funcs.setdefault(None, []).append(_swap)
    try:
        yield
    finally:
        app.app.before_request_funcs[None].remove(_swap)


def test_read_share_cannot_edit(app, client, upload, host):
    if not host.has_service("ownership"):
        pytest.skip("ownership module off")
    made = []
    for name in ("alb_owner", "alb_reader"):
        r = client.post("/api/auth/users/create", json={"username": name, "password": "pw", "role": "custom"})
        assert r.status_code == 200, r.get_json()
        made.append(r.get_json()["user"])
    owner, reader = made
    try:
        pic = upload("alb_shared.png", seed=310, scope="public")
        with _as(app, owner):
            assert _post(client, "/api/albums/create", name="SharedAlb", files=[pic]).status_code == 200
            assert _post(client, "/api/albums/share", album="SharedAlb",
                         shares=[{"user_id": reader["id"], "level": "read"}]).status_code == 200
        with _as(app, reader):
            assert _albums(client)["SharedAlb"]["level"] == "read"
            assert _post(client, "/api/albums/describe", album="SharedAlb", description="x").status_code == 403
            assert _post(client, "/api/albums/sort", album="SharedAlb", sort="added").status_code == 403
            assert _post(client, "/api/albums/order", album="SharedAlb", rel_paths=[pic]).status_code == 403
        with _as(app, owner):
            assert _post(client, "/api/albums/describe", album="SharedAlb", description="mine").status_code == 200
            assert _post(client, "/api/albums/sort", album="SharedAlb", sort="added").status_code == 200
            assert _post(client, "/api/albums/delete", name="SharedAlb").status_code == 200
    finally:
        for u in made:
            client.post("/api/auth/users/delete", json={"id": u["id"]})
