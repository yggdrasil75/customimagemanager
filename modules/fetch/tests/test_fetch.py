"""! @file
@brief The fetch queue hands what a fetcher downloads to the upload spool without
keeping a second copy: a file in the job's own download scratch is moved, so a long
fetch (or one waiting for disk space) never holds its files twice.
    ./run_tests.sh modules/fetch
"""
import io
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


@pytest.fixture
def opt_fetcher(app):
    """! @brief A fetcher with an options schema and presets; records what each run got."""
    reg = app.module_host.get_service("fetch")
    if reg is None:
        pytest.skip("fetch module off")
    runs = []

    def fetch(target, tmpdir, on_file=None, options=None):
        runs.append(dict(options or {}))
        return None
    schema = [
        {"key": "mode", "label": "Mode", "kind": "select", "default": "video",
         "options": [{"value": "video", "label": "V"}, {"value": "audio", "label": "A"}]},
        {"key": "subs", "label": "Subs", "kind": "toggle", "default": False},
        {"key": "items", "label": "Items", "kind": "text", "default": ""},
        {"key": "n", "label": "N", "kind": "number", "default": 3, "min": 1, "max": 10},
    ]
    reg.register({"id": "opt_test", "label": "Opt", "available": lambda: True, "priority": 100,
                  "handles": lambda t: str(t).startswith("opt://"), "target_key": lambda t: "opt",
                  "fetch": fetch, "options": schema,
                  "check_options": lambda o: "items must not be 'bad'" if o.get("items") == "bad" else None,
                  "presets": [{"id": "song", "label": "Song", "values": {"mode": "audio", "bogus": 1},
                               "sections": ["music"]}]})
    saved = dict(app.module_host.config.get("fetch_defaults") or {})
    yield reg, runs
    reg._fetchers.pop("opt_test", None)
    app.module_host.set_config("fetch_defaults", saved)
    app._db().execute("DELETE FROM fetch_queue WHERE target LIKE 'opt://%'")
    app._db().execute("DELETE FROM fetch_watch WHERE target LIKE 'opt://%'")
    app._db().commit()


def test_clean_options_keeps_what_fits_the_schema():
    from modules.fetch.module import clean_options
    sc = [{"key": "m", "kind": "select", "options": [{"value": "a"}, {"value": "b"}]},
          {"key": "t", "kind": "toggle"}, {"key": "n", "kind": "number", "min": 1, "max": 5},
          {"key": "s", "kind": "text"}]
    assert clean_options(sc, {"m": "c", "t": "on", "n": "9", "s": "  x ", "z": 1}) == \
        {"t": True, "n": 5, "s": "x"}


def test_job_options_layer_defaults_saved_and_chosen(app, client, opt_fetcher):
    reg, runs = opt_fetcher
    j = client.get("/api/fetch/fetchers").get_json()
    me = next(f for f in j["fetchers"] if f["id"] == "opt_test")
    assert [x["key"] for x in me["options"]] == ["mode", "subs", "items", "n"]
    assert me["defaults"] == {"mode": "video", "subs": False, "items": "", "n": 3}
    assert me["presets"][0]["values"] == {"mode": "audio"}               # unknown keys dropped
    # saved defaults: only what differs from the built-in ones is stored
    r = client.post("/api/fetch/defaults", json={"fetcher": "opt_test", "values": {"subs": True, "mode": "video"}})
    assert r.get_json()["success"]
    assert app.module_host.config["fetch_defaults"]["opt_test"] == {"subs": True}
    assert client.post("/api/fetch/defaults", json={"fetcher": "opt_test",
                                                    "values": {"items": "bad"}}).status_code == 400
    # a download: its own choices over the saved over the built-in defaults
    r = client.post("/api/fetch/add", json={"targets": ["opt://a"], "options": {"mode": "audio", "n": 50}})
    assert r.get_json()["success"]
    qid = app._db().execute("SELECT MAX(id) FROM fetch_queue WHERE target='opt://a'").fetchone()[0]
    reg.run(qid)
    assert runs[-1] == {"mode": "audio", "subs": True, "items": "", "n": 10}
    # retry keeps the job's own options
    client.post("/api/fetch/add", json={"retry_id": qid})
    qid2 = app._db().execute("SELECT MAX(id) FROM fetch_queue WHERE target='opt://a'").fetchone()[0]
    assert qid2 != qid
    reg.run(qid2)
    assert runs[-1]["mode"] == "audio"
    assert client.post("/api/fetch/add", json={"targets": ["opt://b"], "options": {"items": "bad"}}).status_code == 400
    # reset goes back to the built-in defaults
    client.post("/api/fetch/defaults", json={"fetcher": "opt_test", "reset": True})
    assert "opt_test" not in app.module_host.config["fetch_defaults"]


def test_fetcher_picked_and_watch_keep_options(app, client, opt_fetcher, fake_fetcher):
    reg, runs = opt_fetcher
    # a fetcher picked in the dialog takes a URL another one would get
    r = client.post("/api/fetch/add", json={"targets": ["fake://zz"], "fetcher": "opt_test",
                                           "options": {"mode": "audio"}}).get_json()
    assert r["success"] and r["fetchers"] == {"fake://zz": "Opt"}
    row = app._db().execute("SELECT fetcher, options FROM fetch_queue WHERE target='fake://zz'").fetchone()
    assert row["fetcher"] == "opt_test" and '"audio"' in row["options"]
    assert client.post("/api/fetch/add", json={"targets": ["x"], "fetcher": "nope"}).status_code == 400
    app._db().execute("DELETE FROM fetch_queue WHERE target='fake://zz'"); app._db().commit()
    # a watch re-queues with its options
    assert client.post("/api/fetch/watch", json={"targets": ["opt://w"], "every_h": 1,
                                                 "options": {"mode": "audio"}}).get_json()["added"] == 1
    wid = app._db().execute("SELECT id FROM fetch_watch WHERE target='opt://w'").fetchone()[0]
    client.post("/api/fetch/watch/run", json={"id": wid})
    row = app._db().execute("SELECT fetcher, options FROM fetch_queue WHERE target='opt://w'").fetchone()
    assert row["fetcher"] == "opt_test" and '"audio"' in row["options"]


def test_priority_may_depend_on_the_target(app):
    reg = app.module_host.get_service("fetch")
    if reg is None:
        pytest.skip("fetch module off")
    saved = dict(reg._fetchers)
    try:
        reg._fetchers.clear()
        reg.register({"id": "a", "available": lambda: True, "handles": lambda t: True, "priority": 0})
        reg.register({"id": "b", "available": lambda: True, "handles": lambda t: True,
                      "priority": lambda t: 10 if "special" in t else -10})
        assert reg.for_target("http://special/x")["id"] == "b"
        assert reg.for_target("http://other/x")["id"] == "a"
    finally:
        reg._fetchers.clear(); reg._fetchers.update(saved)


def test_subtitles_and_store_as_is_reach_the_library(app, client, tmp_path):
    """! @brief A fetcher's subtitle file is stored as sent beside its video (outside any
    section), and a video marked _store_as_is keeps its container even when Settings >
    Media converts videos."""
    import json, shutil, subprocess
    from modules.encoding import convert
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    clip = tmp_path / "c.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=64x64:d=1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)], check=True)
    saved = convert.media_prefs()
    convert.set_media_prefs(dict(saved, video={"target": ".mp4", "mode": "all"}))
    made = []
    try:
        def up(name, data, meta):
            r = client.post("/api/upload", data={"file": (io.BytesIO(data), name), "mode": "sync",
                                                 "folder": "fetch_side", "metadata": json.dumps(meta)},
                            content_type="multipart/form-data")
            j = r.get_json()
            assert r.status_code == 200 and j["success"], j
            made.append(j["filename"])
            return j
        v = up("clip.mkv", clip.read_bytes(), {"_store_as_is": True})
        assert v["filename"] == "fetch_side/clip.mkv"                      # not converted to .mp4
        s = up("clip.srt", b"1\n00:00:00,000 --> 00:00:01,000\nhi\n", {"_sidecar": True})
        assert s["filename"] == "fetch_side/clip.srt" and s.get("sidecar")
        assert os.path.isfile(os.path.join(app.MEDIA_DIR, "fetch_side", "clip.srt"))
        assert ".srt" in app.mt.related_exts(os.path.join(app.MEDIA_DIR, "fetch_side", "clip.mkv"))
        # without the flag a subtitle is still refused outside a section
        r = client.post("/api/upload", data={"file": (io.BytesIO(b"x"), "other.srt"), "mode": "sync",
                                             "folder": "fetch_side"}, content_type="multipart/form-data")
        assert r.status_code >= 400
    finally:
        convert.set_media_prefs(saved)
        client.post("/api/delete", json={"filename": "fetch_side/clip.mkv"})
        for f in ("clip.srt",):
            p = os.path.join(app.MEDIA_DIR, "fetch_side", f)
            if os.path.exists(p):
                os.remove(p)