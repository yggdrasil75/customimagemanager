"""! @file
@brief The fetch queue hands what a fetcher downloads to the upload spool without
keeping a second copy: a file in the job's own download scratch is moved, so a long
fetch (or one waiting for disk space) never holds its files twice.
    ./run_tests.sh modules/fetch
"""
import os

import pytest

from cimtest import drain_uploads, png_bytes


@pytest.fixture
def fake_fetcher(app):
    """! @brief A fetcher for fake:// targets writing three PNGs into its scratch dir;
    it notes whether each file was still there after on_file."""
    reg = app.module_host.get_service("fetch")
    if reg is None:
        pytest.skip("fetch module off")
    seen = {"left": [], "tmp": None}

    def fetch(target, tmpdir, on_file=None):
        seen["tmp"] = tmpdir
        for i in range(3):
            p = os.path.join(tmpdir, f"f{i}.png")
            with open(p, "wb") as f:
                f.write(png_bytes(seed=8100 + i))
            on_file(p, {"category": "fake"})
            seen["left"].append(os.path.exists(p))
        return None
    reg.register({"id": "fake_test", "label": "fake", "available": lambda: True,
                  "handles": lambda t: str(t).startswith("fake://"), "target_key": lambda t: "fake",
                  "fetch": fetch, "priority": 100})
    yield reg, seen
    reg._fetchers.pop("fake_test", None)
    db = app._db()
    for r in db.execute("SELECT rel_path FROM files WHERE rel_path LIKE 'fetch_test/%'").fetchall():
        app.app.test_client().post("/api/delete", json={"filename": r["rel_path"]})
    db.execute("DELETE FROM fetch_queue WHERE target LIKE 'fake://%'")
    db.commit()


def test_fetched_files_are_moved_into_the_spool(app, client, fake_fetcher):
    reg, seen = fake_fetcher
    r = client.post("/api/fetch/add", json={"targets": ["fake://album"], "folder": "fetch_test"})
    assert r.get_json()["success"], r.get_json()
    qid = app._db().execute("SELECT MAX(id) FROM fetch_queue WHERE target='fake://album'").fetchone()[0]
    spools = {row["spool_path"] for row in app._db().execute(
        "SELECT spool_path FROM upload_queue WHERE status='pending'")}
    row = reg.run(qid)
    assert row["status"] == "done", row
    assert seen["left"] == [False, False, False]          # no copy left behind per file
    assert not os.path.exists(seen["tmp"])                 # and the scratch is gone
    new = [r["spool_path"] for r in app._db().execute(
        "SELECT spool_path FROM upload_queue WHERE status='pending'") if r["spool_path"] not in spools]
    assert len(new) == 3 and all(os.path.exists(p) for p in new)
    drain_uploads(app)
    assert not any(os.path.exists(p) for p in new)          # ingested: spool files removed
    got = app._db().execute("SELECT COUNT(*) FROM files WHERE rel_path LIKE 'fetch_test/%'").fetchone()[0]
    assert got == 3

def test_the_fetch_dialog_is_the_fetch_modules(app, client):
    """! @brief The dialog is on the page with the fetch module alone (gallery-dl may be off)
    and its gallery-dl parts are marked so they hide without that fetcher."""
    if app.module_host.get_service("fetch") is None:
        pytest.skip("fetch module off")
    html = client.get("/").get_data(as_text=True)
    assert 'id="gdl_modal"' in html and 'id="btn_gdl"' in html
    assert html.count('data-fetcher-only="gallerydl"') >= 4
    assert not any(m["module_id"] == "gallerydl" for m in app.module_host.app_modals)


def test_which_and_add_name_the_fetcher(app, client, fake_fetcher):
    w = client.post("/api/fetch/which", json={"target": "fake://x"}).get_json()
    assert w["success"] and w["id"] == "fake_test" and w["label"] == "fake"
    w = client.post("/api/fetch/which", json={"target": "nothing-handles-this"}).get_json()
    assert w["success"] and w["id"] is None
    r = client.post("/api/fetch/add", json={"targets": ["fake://y", "nothing-handles-this"]}).get_json()
    assert r["success"] and r["added"] == 2
    assert r["fetchers"] == {"fake://y": "fake", "nothing-handles-this": None}
    app._db().execute("DELETE FROM fetch_queue WHERE target='nothing-handles-this'")
    app._db().commit()


def test_ytdlp_hosts_win_over_gallery_dl(app):
    """! @brief A host in the yt-dlp setting goes to yt-dlp even when gallery-dl (registered
    first, same default priority) also has an extractor for it."""
    reg = app.module_host.get_service("fetch")
    if reg is None or reg.get("ytdlp") is None:
        pytest.skip("ytdlp module off")
    yt = reg.get("ytdlp")
    saved = dict(reg._fetchers)
    try:
        reg._fetchers.clear()
        reg.register({"id": "greedy", "label": "greedy", "available": lambda: True,
                      "handles": lambda t: True})
        reg.register(dict(yt, available=lambda: True))
        assert reg.for_target("https://www.youtube.com/watch?v=x")["id"] == "ytdlp"
        assert reg.for_target("https://example.org/")["id"] == "greedy"
    finally:
        reg._fetchers.clear()
        reg._fetchers.update(saved)


def test_folder_template_names_a_file_only_with_an_extension():
    """! @brief "music/{uploader}" is a folder per uploader (it used to name every file of
    that uploader the same); a last segment with an extension still names the file."""
    from modules.fetch.module import expand_path
    m = {"uploader": "Some Artist", "title": "Song", "id": "x1"}
    assert expand_path("music/{uploader}", m, "vid.mp4") == ("music/Some Artist", "vid.mp4")
    assert expand_path("music/{uploader}/{title}.{ext}", m, "vid.mp4") == ("music/Some Artist", "Song.mp4")
    assert expand_path("art/{id}_{original_name}.{ext}", m, "a.png") == ("art", "x1_a.png")
    assert expand_path("{title}.mp4", m, "vid.webm") == ("", "Song.mp4")
    assert expand_path("music", m, "vid.mp4") == ("music", "vid.mp4")