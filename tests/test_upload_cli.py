"""! @file
@brief upload.py against a fake server: --dest into a library section (every
file goes, Plex layout warnings, the /api/sections fallback), --delete-source
(its gating, every delete / keep decision, the sidecar rule) and --dry-run.
No server runs: a Session subclass answers every HTTP call from a script.
"""
import hashlib
import io
import json
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import upload as upcli  # noqa: E402

_FILENAME_RE = re.compile(rb'filename="([^"]*)"')
_RealSession = upcli.Session


class FakeSession(_RealSession):
    """! @brief upload.Session with the wire replaced by a script.
    @param replies   {filename: [(status, body_dict) | TransportError, ...]} for
                     POST /api/upload, consumed in order (the last one repeats).
    @param sections  the GET /api/sections answer: a list of rows, or None for a
                     server without the endpoint (404).
    """
    instances = []

    def __init__(self, *a, **kw):
        _RealSession.__init__(self, *a, **kw)
        self.replies = dict(FakeSession.script)
        self.sections = FakeSession.sections
        self.posted = []      # filenames sent to /api/upload, in order
        self.headers_seen = {}
        self.calls = []
        FakeSession.instances.append(self)

    script = {}
    sections = None

    def raw(self, method, path, body=None, headers=None, timeout=60):
        self.calls.append((method, path))
        if path == "/api/auth/config":
            return 200, json.dumps({"enabled": False, "mode": "none"}).encode()
        if path == "/api/upload/config":
            return 200, json.dumps({"success": True, "sessions": False, "validate": False}).encode()
        if path == "/api/sections":
            if self.sections is None:
                return 404, b"not found"
            return 200, json.dumps({"success": True, "sections": self.sections}).encode()
        if path == "/api/auth/logout":
            return 200, b"{}"
        if path == "/api/upload" and method == "POST":
            data = body.read() if hasattr(body, "read") else body
            fname = _FILENAME_RE.search(data).group(1).decode()
            self.posted.append(fname)
            self.headers_seen[fname] = dict(headers or {})
            script = self.replies.get(fname) or [(200, {"success": True, "filename": fname})]
            answer = script[0] if len(script) == 1 else script.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer[0], json.dumps(answer[1]).encode()
        return 404, b"{}"


@pytest.fixture
def fake(monkeypatch):
    """! @brief Install FakeSession as upload.Session; returns a configure(script, sections) hook."""
    FakeSession.instances = []
    FakeSession.script = {}
    FakeSession.sections = None
    monkeypatch.setattr(upcli, "Session", FakeSession)

    def configure(script=None, sections=None):
        FakeSession.script = script or {}
        FakeSession.sections = sections
    return configure


def _write(root, rel, data=b"x"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _run(src, **kw):
    args = dict(server_url="http://fake", workers=1, max_attempts=2, initial_backoff=0,
                verbose_dupes=False)
    args.update(kw)
    return upcli.bulk_upload(str(src), **args)


# -- sections: every file goes -----------------------------------------------------
def test_section_sends_every_file_and_folds_the_notes(tmp_path, fake, capsys):
    """! @brief Inside a section nothing is refused: the film, its art, its sidecars, a
    stray .flac and a .json all travel as files; only the .txt note goes as metadata."""
    movie = tmp_path / "Heat (1995)"
    for name in ("Heat (1995).mkv", "poster.jpg", "Heat (1995).nfo", "Heat (1995).en.srt",
                 "song.flac", "notes.json", "Heat (1995).txt"):
        _write(movie, name)
    fake()
    assert _run(tmp_path, dest="movies") == 0
    s = FakeSession.instances[-1]
    assert sorted(s.posted) == ["Heat (1995).en.srt", "Heat (1995).mkv", "Heat (1995).nfo",
                                "notes.json", "poster.jpg", "song.flac"]
    out = capsys.readouterr().out
    assert "Movies section: every file is sent as it is" in out
    assert "book, image, video are its items" in out
    assert "Not sent" not in out and "does not take" not in out
    # the .txt is folded into its film's metadata, never a file of its own
    assert "Heat (1995).txt" not in out


def test_music_section_takes_everything_beside_the_tracks(tmp_path, fake):
    album = tmp_path / "Artist" / "Album"
    names = ("01 - Track.flac", "cover.jpg", "booklet.pdf", "album.cue", "rip.log",
             "Album.m3u", "clip.mkv", "Album.srt", "notes.txt", "01 - Track.xmp")
    for name in names:
        _write(album, name)
    fake()
    assert _run(tmp_path, dest="music/") == 0
    s = FakeSession.instances[-1]
    # a music video, a foreign subtitle: sent too; .txt / .xmp travel as metadata
    assert sorted(s.posted) == ["01 - Track.flac", "Album.m3u", "Album.srt", "album.cue",
                                "booklet.pdf", "clip.mkv", "cover.jpg", "rip.log"]


def test_section_allows_only_withholds_metadata_files():
    assert upcli.section_allows("movies", {"video"}, "song.flac")
    assert upcli.section_allows("movies", {"video"}, "notes.weird")
    assert upcli.section_allows("music", {"audio"}, "clip.mkv")
    assert not upcli.section_allows("movies", {"video"}, "classes.txt")
    assert not upcli.section_allows("movies", {"video"}, "Heat (1995).txt")
    assert not upcli.section_allows("movies", {"video"}, "Heat (1995).XMP")


def test_server_sections_win_over_the_fallback(tmp_path, fake):
    _write(tmp_path, "a.jpg")
    _write(tmp_path, "b.mp3")
    _write(tmp_path, "c.nfo")
    _write(tmp_path, "d.weird")
    # a custom section only the server knows: inside it everything goes, and the
    # row's sidecars are learned
    fake(sections=[{"id": "photos", "label": "Photos", "kinds": ["image"], "folders": [],
                    "sidecars": [".nfo", ".PHO"]}])
    assert _run(tmp_path, dest="photos/2024") == 0
    assert FakeSession.instances[-1].posted == ["a.jpg", "b.mp3", "c.nfo", "d.weird"]
    assert ".pho" in upcli.SIDECAR_EXTENSIONS
    assert _run(tmp_path, dest="music") == 0  # the server has no music section: a plain folder
    assert FakeSession.instances[-1].posted == ["a.jpg", "b.mp3"]
    # the fallback has the three names with their spec kinds and sidecars
    fake(sections=None)
    assert _run(tmp_path, dest="music") == 0
    assert FakeSession.instances[-1].posted == ["a.jpg", "b.mp3", "c.nfo", "d.weird"]
    assert _run(tmp_path, dest="photos") == 0  # not a section: media files only
    assert sorted(FakeSession.instances[-1].posted) == ["a.jpg", "b.mp3"]


def test_fetch_sections_reads_kinds_and_sidecars(fake):
    fake(sections=[{"id": "Music", "label": "Music", "kinds": ["audio", "video"],
                    "sidecars": [".cue", ".LOG"]},
                   {"id": "", "label": "ignored"}, "junk"])
    secs = upcli.fetch_sections(upcli.Session("http://fake"))
    assert set(secs) == {"music"}
    assert secs["music"] == {"label": "Music", "kinds": {"audio", "video"}, "sidecars": {".cue", ".log"}}
    fake(sections=None)
    secs = upcli.fetch_sections(upcli.Session("http://fake"))
    assert set(secs) == {"music", "movies", "tv"}
    assert "video" in secs["music"]["kinds"] and ".cue" in secs["music"]["sidecars"]
    assert ".srt" in secs["movies"]["sidecars"] and ".srt" in secs["tv"]["sidecars"]


def test_personal_section_folder_and_dest_section_helper():
    secs = {"music": {"label": "Music", "kinds": {"audio"}}}
    assert upcli.dest_section("music", secs) == ("music", "")
    assert upcli.dest_section("Music/Artist/Album", secs) == ("music", "Artist/Album")
    assert upcli.dest_section("users/ann/music/Artist", secs) == ("music", "Artist")
    assert upcli.dest_section("users/ann/photos", secs) == (None, "users/ann/photos")
    assert upcli.dest_section("photos/music", secs) == (None, "photos/music")
    assert upcli.dest_section("", secs) == (None, "")


def test_outside_a_section_nothing_changes(tmp_path, fake):
    _write(tmp_path, "a.jpg")
    _write(tmp_path, "b.mp3")
    _write(tmp_path, "c.nfo")
    fake()
    assert _run(tmp_path, dest="holiday") == 0
    assert sorted(FakeSession.instances[-1].posted) == ["a.jpg", "b.mp3"]


# -- sections: layout warnings ---------------------------------------------------
def _warnings(out):
    return [l for l in out.splitlines() if "[!] layout:" in l]


def test_layout_warning_once_per_folder_never_blocks(tmp_path, fake, capsys):
    loose = tmp_path / "Loose"
    _write(loose, "a.flac")
    _write(loose, "b.flac")
    _write(tmp_path / "Artist" / "Album", "01 - Track.flac")
    _write(tmp_path / "Artist", "artist.jpg")
    fake()
    assert _run(tmp_path, dest="music") == 0
    err = capsys.readouterr().err
    w = _warnings(err)
    assert len(w) == 1 and "Loose" in w[0] and "Artist/Album" in w[0]
    assert sorted(FakeSession.instances[-1].posted) == ["01 - Track.flac", "a.flac", "artist.jpg", "b.flac"]


def test_layout_patterns_per_section():
    lw = upcli.layout_warning
    # music: tracks two levels down, art anywhere
    assert lw("music", "Artist/Album", ["01.flac", "cover.jpg"]) == ""
    assert lw("music", "Artist", ["artist.jpg"]) == ""
    assert "Artist/Album" in lw("music", "Artist", ["01.flac"])
    assert lw("music", "A/B/C", ["01.flac"]) != ""
    # movies: Title (Year)/ with the film, extras one level down
    assert lw("movies", "Heat (1995)", ["Heat (1995).mkv", "poster.jpg", "Heat (1995).nfo"]) == ""
    assert lw("movies", "Heat (1995)/Featurettes", ["making of.mkv"]) == ""
    assert "Title (Year)" in lw("movies", "Heat", ["Heat.mkv"])
    assert "Title (Year)" in lw("movies", "", ["Heat (1995).mkv"])
    assert lw("movies", "Heat (1995)/a/b", ["x.mkv"]) != ""
    assert lw("movies", "whatever", ["poster.jpg"]) == ""
    # tv: Show (Year)/Season 01/ and an sXXeYY marker
    assert lw("tv", "Show (2019)/Season 01", ["Show - s01e01 - Pilot.mkv", "season01-poster.jpg"]) == ""
    assert lw("tv", "Show (2019)/Season 1", ["Show S01E02.mkv"]) == ""
    assert lw("tv", "Show (2019)/Specials", ["Show - s00e01.mkv"]) == ""
    assert "Season 01" in lw("tv", "Show (2019)", ["Show - s01e01.mkv"])
    assert "Season 01" in lw("tv", "Show (2019)/Extras", ["x s01e01.mkv"])
    assert "s01e01" in lw("tv", "Show (2019)/Season 01", ["Pilot.mkv"])
    assert lw("tv", "Show (2019)", ["poster.jpg"]) == ""
    assert lw("other", "x", ["a.mkv"]) == ""


def test_dest_inner_path_counts_toward_the_layout(tmp_path, fake, capsys):
    _write(tmp_path / "Album", "01.flac")
    fake()
    assert _run(tmp_path, dest="music/Artist") == 0
    assert _warnings(capsys.readouterr().err) == []
    assert _run(tmp_path, dest="music") == 0
    assert len(_warnings(capsys.readouterr().err)) == 1


# -- delete-source: gating -------------------------------------------------------
def test_delete_source_needs_sync_mode(tmp_path, fake, capsys):
    p = _write(tmp_path, "a.jpg")
    fake()
    for mode in ("auto", "spool"):
        assert _run(tmp_path, mode=mode, delete_source=True) == 2
    assert "--mode sync" in capsys.readouterr().out
    assert FakeSession.instances == [] and p.exists()


def test_delete_source_implies_validate(tmp_path, fake, capsys):
    data = b"jpeg bytes"
    _write(tmp_path, "a.jpg", data)
    fake({"a.jpg": [(200, {"success": True, "filename": "a.jxl"})]})
    assert _run(tmp_path, mode="sync", delete_source=True) == 0
    s = FakeSession.instances[-1]
    assert s.headers_seen["a.jpg"]["X-Content-SHA256"] == _sha(data)
    assert "--validate" in capsys.readouterr().out


# -- delete-source: every decision -----------------------------------------------
def _result(outcome, local="a" * 64, receipt="a" * 64, status=200, attempts=1, existing="x.jxl"):
    return upcli.UploadResult("/f.jpg", outcome, "", attempts=attempts, status=status,
                              local_sha=local, receipt_sha=receipt,
                              existing_file=existing if outcome == upcli.Outcome.DUPLICATE else None)


def test_delete_decision_table():
    d = upcli.delete_decision
    S, D, Q, K, F = (upcli.Outcome.SUCCESS, upcli.Outcome.DUPLICATE, upcli.Outcome.QUEUED,
                     upcli.Outcome.SKIPPED, upcli.Outcome.FAILED)
    assert d(_result(S))[0] is True
    assert d(_result(D))[0] is True
    # never: queued, failed, rejected, retried, no local sha, not a 200
    assert d(_result(Q))[0] is False
    assert d(_result(F))[0] is False
    assert d(_result(K))[0] is False
    assert d(_result(S, attempts=2)) == (False, "needed a retry")
    assert d(_result(D, attempts=2))[0] is False
    assert d(_result(S, local=""))[0] is False
    assert d(_result(S, status=202))[0] is False
    # a receipt without a sha, or with another file's sha
    assert d(_result(S, receipt="")) == (False, "receipt reports no sha256")
    assert d(_result(S, receipt="b" * 64))[0] is False
    assert d(_result(D, receipt=""))[0] is False
    assert d(_result(D, receipt="b" * 64))[0] is False
    assert d(_result(D, existing=None))[0] is False


def test_receipt_sha_fields():
    good = "0" * 64
    assert upcli.receipt_sha({"sha256": good.upper()}) == good
    assert upcli.receipt_sha({"source_sha256": good}) == good
    assert upcli.receipt_sha({"existing_sha256": good}) == ""
    assert upcli.receipt_sha({"existing_sha256": good}, duplicate=True) == good
    assert upcli.receipt_sha({"sha256": "nope"}) == ""
    assert upcli.receipt_sha({}) == ""


def test_delete_source_end_to_end(tmp_path, fake, capsys):
    files = {}
    for name in ("ok.jpg", "nosha.jpg", "wrongsha.jpg", "dup.jpg", "dupnosha.jpg", "dupnofile.jpg",
                 "queued.jpg", "retried.jpg", "failed.jpg", "rejected.jpg", "dup409.jpg"):
        files[name] = name.encode() * 3
        _write(tmp_path, name, files[name])
    sha = dict((n, _sha(d)) for n, d in files.items())
    fake({
        "ok.jpg":       [(200, {"success": True, "filename": "ok.jxl", "sha256": sha["ok.jpg"]})],
        "nosha.jpg":    [(200, {"success": True, "filename": "nosha.jxl"})],
        "wrongsha.jpg": [(200, {"success": True, "filename": "w.jxl", "sha256": "f" * 64})],
        "dup.jpg":      [(200, {"success": True, "duplicate": True, "filename": "old.jxl",
                                "existing_file": "old.jxl", "sha256": sha["dup.jpg"]})],
        "dupnosha.jpg": [(200, {"success": True, "duplicate": True, "existing_file": "old2.jxl"})],
        "dupnofile.jpg": [(200, {"success": True, "duplicate": True, "sha256": sha["dupnofile.jpg"]})],
        "queued.jpg":   [(202, {"success": True, "filename": "q.jxl", "queue_id": 3,
                                "sha256": sha["queued.jpg"]})],
        "retried.jpg":  [(500, {"success": False, "error_code": "server_error", "error": "hiccup"}),
                         (200, {"success": True, "filename": "r.jxl", "sha256": sha["retried.jpg"]})],
        "failed.jpg":   [upcli.TransportError("reset")],
        "rejected.jpg": [(422, {"success": False, "error_code": "conversion_failed", "error": "bad"})],
        "dup409.jpg":   [(409, {"success": False, "error_code": "exact_duplicate",
                                "existing_file": "old3.jxl", "sha256": sha["dup409.jpg"]})],
    })
    assert _run(tmp_path, mode="sync", delete_source=True) == 1  # failed.jpg
    left = sorted(p.name for p in tmp_path.iterdir())
    assert left == ["dupnofile.jpg", "dupnosha.jpg", "failed.jpg", "nosha.jpg", "queued.jpg",
                    "rejected.jpg", "retried.jpg", "wrongsha.jpg"]
    out = capsys.readouterr().out
    assert "Deleted:    3" in out and "Kept:       8" in out
    assert "retried.jpg: needed a retry" in out
    assert "queued.jpg: queued (202)" in out
    assert "nosha.jpg: receipt reports no sha256" in out


def test_without_delete_source_nothing_is_removed(tmp_path, fake, capsys):
    data = b"abc"
    _write(tmp_path, "ok.jpg", data)
    fake({"ok.jpg": [(200, {"success": True, "filename": "ok.jxl", "sha256": _sha(data)})]})
    assert _run(tmp_path, mode="sync", validate=True) == 0
    assert (tmp_path / "ok.jpg").exists()
    assert "Deleted:" not in capsys.readouterr().out


# -- delete-source: sidecars -----------------------------------------------------
def test_sidecars_go_only_with_their_media_file(tmp_path, fake, capsys):
    m = tmp_path / "Heat (1995)"
    gone = b"heat"
    kept = b"kept"
    _write(m, "Heat (1995).mkv", gone)
    _write(m, "Heat (1995).nfo", b"<movie/>")
    _write(m, "Heat (1995).en.srt", b"1")
    _write(m, "Heat (1995).txt", b"a note, folded into metadata")
    _write(m, "Heat (1995).xmp", b"<x/>")
    k = tmp_path / "Kept (2000)"
    _write(k, "Kept (2000).mkv", kept)
    _write(k, "Kept (2000).nfo", b"<movie/>")
    _write(k, "Kept (2000).txt", b"note")
    _write(tmp_path, "stray.txt", b"nobody's")
    _write(tmp_path, "stray.nfo", b"nobody's")
    fake({
        "Heat (1995).mkv":    [(200, {"success": True, "filename": "h.mkv", "sha256": _sha(gone)})],
        "Heat (1995).nfo":    [(200, {"success": True, "filename": "h.nfo", "sha256": _sha(b"<movie/>")})],
        "Heat (1995).en.srt": [(200, {"success": True, "filename": "h.en.srt", "sha256": _sha(b"1")})],
        "Kept (2000).mkv":    [(200, {"success": True, "filename": "k.mkv"})],  # no sha: kept
        "Kept (2000).nfo":    [(200, {"success": True, "filename": "k.nfo", "sha256": _sha(b"<movie/>")})],
        "stray.nfo":          [(200, {"success": True, "filename": "s.nfo", "sha256": _sha(b"nobody's")})],
    })
    assert _run(tmp_path, dest="movies", mode="sync", delete_source=True) == 0
    assert sorted(p.name for p in m.iterdir()) == []
    assert sorted(p.name for p in k.iterdir()) == ["Kept (2000).mkv", "Kept (2000).nfo", "Kept (2000).txt"]
    assert (tmp_path / "stray.txt").exists() and (tmp_path / "stray.nfo").exists()
    out = capsys.readouterr().out
    assert "Deleted:    5" in out
    assert "Kept (2000).nfo: its media file Kept (2000).mkv was kept" in out
    assert "stray.nfo: sidecar without a media file" in out


def test_sent_sidecar_needs_its_own_receipt_too(tmp_path, fake):
    m = tmp_path / "Heat (1995)"
    data = b"heat"
    _write(m, "Heat (1995).mkv", data)
    _write(m, "Heat (1995).nfo", b"<movie/>")
    _write(m, "Heat (1995).txt", b"note")
    fake({
        "Heat (1995).mkv": [(200, {"success": True, "filename": "h.mkv", "sha256": _sha(data)})],
        "Heat (1995).nfo": [(202, {"success": True, "filename": "h.nfo", "queue_id": 1})],
    })
    assert _run(tmp_path, dest="movies", mode="sync", delete_source=True) == 0
    # the film and its metadata note went; the queued .nfo stays until it has a receipt
    assert sorted(p.name for p in m.iterdir()) == ["Heat (1995).nfo"]


def test_sidecar_owner_matching():
    owners = {"heat (1995)": "/m/Heat (1995).mkv"}
    assert upcli.sidecar_owner("/m/Heat (1995).nfo", owners) == "/m/Heat (1995).mkv"
    assert upcli.sidecar_owner("/m/Heat (1995).en.srt", owners) == "/m/Heat (1995).mkv"
    assert upcli.sidecar_owner("/m/HEAT (1995).txt", owners) == "/m/Heat (1995).mkv"
    assert upcli.sidecar_owner("/m/other.nfo", owners) is None
    assert upcli.is_sidecar("x.nfo") and upcli.is_sidecar("x.txt") and upcli.is_sidecar("x.xmp")
    assert not upcli.is_sidecar("classes.txt") and not upcli.is_sidecar("x.jpg")


# -- dry run -----------------------------------------------------------------------
def test_dry_run_sends_and_deletes_nothing(tmp_path, fake, capsys):
    m = tmp_path / "Heat"
    _write(m, "Heat.mkv", b"a")
    _write(m, "Heat.nfo", b"b")
    _write(m, "song.flac", b"c")
    fake({"Heat.mkv": [(200, {"success": True, "filename": "h.mkv", "sha256": _sha(b"a")})]})
    assert _run(tmp_path, dest="movies", mode="sync", delete_source=True, dry_run=True) == 0
    s = FakeSession.instances[-1]
    assert s.posted == [] and ("POST", "/api/upload") not in s.calls
    assert sorted(p.name for p in m.iterdir()) == ["Heat.mkv", "Heat.nfo", "song.flac"]
    out, err = capsys.readouterr()
    assert "Dry run" in out
    assert "would send    Heat/Heat.mkv  ->  movies/Heat/" in out
    assert "would send    Heat/Heat.nfo" in out
    assert "would send    Heat/song.flac" in out
    assert "would not send" not in out
    assert "Would send:     3  (2 media, 1 sidecar)" in out
    assert "Would not send: 0" in out
    assert "Would delete:" in out
    assert len(_warnings(err)) == 1  # the layout check still runs


def test_dry_run_without_a_server(tmp_path, monkeypatch, capsys):
    """! @brief --dry-run reports from the built-in section list when the server is down."""
    _write(tmp_path, "a.mp3")
    _write(tmp_path, "b.mkv")
    _write(tmp_path, "c.nfo")

    class Down(_RealSession):
        def raw(self, *a, **kw):
            raise upcli.TransportError("connection refused")
    monkeypatch.setattr(upcli, "Session", Down)
    assert upcli.bulk_upload(str(tmp_path), "http://down", 1, 1, 0, False, dest="music",
                             dry_run=True) == 0
    out = capsys.readouterr().out
    assert "would send    a.mp3" in out and "would send    b.mkv" in out
    assert "would send    c.nfo" in out and "would not send" not in out
    assert "Would send:     3  (2 media, 1 sidecar)" in out
    assert "built-in section list" in out
    # without --dry-run an unreachable server is still an error
    assert upcli.bulk_upload(str(tmp_path), "http://down", 1, 1, 0, False, dest="music") == 2


def test_help_mentions_the_new_flags(capsys):
    with pytest.raises(SystemExit):
        sys.argv = ["upload.py", "--help"]
        upcli.main()
    out = " ".join(capsys.readouterr().out.split())  # argparse wraps lines
    for word in ("--delete-source", "--dry-run", "--mode sync", "sha256", "Plex layout", "sections"):
        assert word in out


# -- the server side of the sidecar rule -----------------------------------------
def test_server_stores_section_sidecars_as_sent(app, host, client):
    """! @brief Inside a section the core stores its declared sidecars (.nfo / subtitles
    with videos, .cue / .log / .m3u with music) and any file of a type it does not
    handle as they are, with a sha256 in the receipt; a foreign sidecar is never
    refused either. Outside a section a sidecar is no upload, as before."""
    sections = host.library_sections()
    cases = {"movies": [("movies/Heat (1995)", "Heat (1995).nfo"),
                        ("movies/Heat (1995)", "Heat (1995).en.srt"),
                        ("users/ann/movies/Heat (1995)", "Heat (1995).sub"),
                        ("movies/Heat (1995)", "album.cue")],
             "tv": [("tv/Show (2019)/Season 01", "Show - s01e01.vtt"),
                    ("tv/Show (2019)", "x.cue")],
             "music": [("music/Artist/Album", "album.cue"),
                       ("music/Artist/Album", "rip.log"),
                       ("music/Artist/Album", "Album.m3u"),
                       ("music/Artist/Album", "Album.srt")]}
    stored = []
    try:
        for sid, posts in cases.items():
            if sid not in sections:
                continue
            for folder, name in posts:
                r = client.post("/api/upload", data={"file": (io.BytesIO(b"x " + name.encode()), name),
                                                     "folder": folder, "mode": "sync"},
                                content_type="multipart/form-data")
                assert r.status_code == 200, (folder, name, r.get_data(as_text=True))
                j = r.get_json()
                assert j["success"] and j["sidecar"] and j["filename"] == f"{folder}/{name}"
                assert j["sha256"] == _sha(b"x " + name.encode())
                assert os.path.isfile(os.path.join(app.MEDIA_DIR, folder, name))
                stored.append(j["filename"])
        r = client.post("/api/upload", data={"file": (io.BytesIO(b"1"), "x.srt"), "folder": "elsewhere",
                                             "mode": "sync"}, content_type="multipart/form-data")
        assert r.status_code != 200 or not r.get_json().get("success")
    finally:
        for fn in stored:
            client.post("/api/delete", json={"filename": fn, "permanent": True})
