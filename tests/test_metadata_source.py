"""The file is the source of truth. The database is a disposable cache.

Every photo manager that "stores everything in the image" still ends up with
state that only lives in its database — a name written to XMP once and never
read back, a field the reindex refuses to overwrite, a cache row that outlives
the file it mirrors. These tests exist to catch that class of bug for ALL
metadata, not one field at a time:

  1. wipe every DB row about a file, reindex → every read field comes back
  2. change the file behind the app's back (another tool, another machine),
     reindex → every read field follows the FILE, even where the DB row
     already held a different, non-empty value
  3. a cache row that exists but lacks what the file says (the "write once,
     read never" shape) → reindex fills it from the file

They are field-agnostic on purpose: they diff the whole /api/metadata read
packet, so a field added tomorrow is covered the day it's added. Add a field
to VOLATILE only when it is genuinely derived and not stored in the file.
"""
import os
import shutil

import pytest
from cimtest import read_meta, write_meta, box

# Read-packet keys that are legitimately NOT file metadata (derived or per-request).
VOLATILE = {"iqa_score", "iqa_manual", "brisque", "rating_user", "effective_rating",
            "iqa_stars", "rating_iqa", "iqa_model", "iqa_raw", "analysis_pending"}

FULL_REGIONS = [
    box(class_name="face", region_name="jill", confirmed=True,
        cx=.5, cy=.25, w=.12, h=.15),
    box(class_name="person", region_name="", confirmed=False,
        cx=.5, cy=.55, w=.4, h=.8),
    box(class_name="object", region_name="red mug", confirmed=True,
        cx=.2, cy=.7, w=.1, h=.1, region_tags=["mug"], region_description="chipped"),
]


def _packet(client, fn):
    """The read packet with derived noise removed and regions made comparable."""
    m = {k: v for k, v in read_meta(client, fn).items() if k not in VOLATILE}
    m["regions"] = sorted(
        ({k: v for k, v in r.items() if k in ("class_name", "region_name", "region_type",
                                              "confirmed", "cx", "cy", "w", "h",
                                              "region_tags", "region_description")}
         for r in m.get("regions") or []),
        key=lambda r: (r["cx"], r["cy"]))
    return m


def _tables_about_files(db):
    """Every (table, column) that keys rows by a media path — discovered from the
    schema so a table added tomorrow is wiped too."""
    out = []
    for (t,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        cols = {r[1] for r in db.execute(f"PRAGMA table_info({t})").fetchall()}
        for c in ("rel_path", "filename", "path"):
            if c in cols:
                out.append((t, c))
                break
    return out


def _wipe_file_rows(app, fn):
    db = app._db()
    for t, c in _tables_about_files(db):
        db.execute(f"DELETE FROM {t} WHERE {c}=?", (fn,))
    db.commit()
    app._meta_cache_drop(fn)


def _reindex(app, fn):
    app._meta_cache_drop(fn)
    assert app._index_file(fn, force=True) is not False
    app._meta_cache_drop(fn)


def _populate(client, fn, name="jill", desc="on the pier", tags=("beach", "2019")):
    regs = [dict(r) for r in FULL_REGIONS]
    regs[0]["region_name"] = name
    write_meta(client, fn, tags=list(tags), desc=desc, regions=regs)


def _abs(app, fn):
    return app.get_safe_path(app.MEDIA_DIR, fn)


def _sidecar(app, fn):
    return os.path.splitext(_abs(app, fn))[0] + ".xmp"


# ── 1. the DB is disposable ───────────────────────────────────────────────────

def test_wipe_db_reindex_restores_every_field(client, upload, app):
    fn = upload(seed=901)
    _populate(client, fn)
    before = _packet(client, fn)
    assert before["tags"] and before["description"] and before["regions"], before

    _wipe_file_rows(app, fn)
    _reindex(app, fn)

    after = _packet(client, fn)
    missing = {k: (before[k], after.get(k)) for k in before if before[k] != after.get(k)}
    assert not missing, f"fields that only lived in the DB: {missing}"


# ── 2. the file wins, even over a non-empty DB value ─────────────────────────

def test_file_changed_behind_apps_back_wins(client, upload, app):
    """Simulate another tool / another machine editing the metadata: copy A's
    sidecar over B's. B's DB row still holds B's old values (non-empty, so any
    "don't overwrite an in-app edit" shortcut would keep them). After a reindex
    every field must read as A's."""
    a, b = upload("a.png", seed=902), upload("b.png", seed=903)
    _populate(client, a, name="ann", desc="A's description", tags=("alpha",))
    _populate(client, b, name="bob", desc="B's description", tags=("beta",))
    want = _packet(client, a)
    assert _packet(client, b) != want

    sa, sb = _sidecar(app, a), _sidecar(app, b)
    if not os.path.exists(sa):
        pytest.skip("no XMP sidecar written for this media type — nothing to copy")
    shutil.copyfile(sa, sb)
    os.utime(_abs(app, b), None)          # the file changed; the app must notice
    _reindex(app, b)

    got = _packet(client, b)
    diff = {k: (want[k], got.get(k)) for k in want if want[k] != got.get(k)}
    assert not diff, f"DB value survived a file change: {diff}"


def test_cleared_field_in_file_clears_the_db(client, upload, app):
    """Emptying a value in the file is a real edit, not a missing value. The
    reindex must not keep the DB's stale copy."""
    fn = upload(seed=904)
    _populate(client, fn, desc="to be removed", tags=("keep-me",))
    assert _packet(client, fn)["description"] == "to be removed"

    # Clear it in the FILE only: write the empty packet straight to disk, then
    # put the old DB row back so only the file knows the field is gone.
    db = app._db()
    old = db.execute("SELECT * FROM files WHERE rel_path=?", (fn,)).fetchone()
    assert old is not None
    assert app.write_metadata(_abs(app, fn), ["keep-me"], "", [])
    cols = old.keys()
    db.execute(f"INSERT OR REPLACE INTO files({','.join(cols)}) VALUES({','.join('?'*len(cols))})",
               tuple(old))
    db.commit()
    os.utime(_abs(app, fn), None)
    _reindex(app, fn)

    got = _packet(client, fn)
    assert got["description"] == "", f"DB kept a description the file no longer has: {got['description']!r}"
    assert got["tags"] == ["keep-me"]


# ── 3. "write once, read never": a cache row exists but lacks what the file says ──

def test_cache_row_is_refreshed_from_file(client, upload, app):
    """A module cache row (here: face_regions) exists for the box, but without
    the name the file carries. This is the exact shape of the people bug —
    api_face_name wrote the name to XMP and nothing ever read it back. The
    invariant is generic: no cache row may disagree with the file after a
    reindex."""
    fn = upload(seed=905)
    _populate(client, fn, name="jill")
    face = FULL_REGIONS[0]
    db = app._db()
    if not [t for t, _ in _tables_about_files(db) if t == "face_regions"]:
        pytest.skip("people module not loaded")
    db.execute("DELETE FROM face_regions WHERE rel_path=?", (fn,))
    db.execute("INSERT INTO face_regions(rel_path,cx,cy,w,h,name,confirmed,cluster_id) "
               "VALUES(?,?,?,?,?,?,?,?)",
               (fn, round(face["cx"], 5), round(face["cy"], 5), face["w"], face["h"], "", 0, 4242))
    db.commit()
    app._meta_cache_drop(fn)

    _reindex(app, fn)

    row = db.execute("SELECT name, confirmed FROM face_regions WHERE rel_path=?", (fn,)).fetchone()
    assert row is not None
    assert (row[0], bool(row[1])) == ("jill", True), \
        f"face_regions cache disagrees with the file: name={row[0]!r} confirmed={row[1]!r}"
    names = [r["region_name"] for r in _packet(client, fn)["regions"] if r["class_name"] == "face"]
    assert names == ["jill"], names


def test_class_label_in_name_slot_is_not_a_name(client, upload, app):
    """A legacy writer put the class ("face") in mwg-rs:Name. That is not a
    person and must never become one."""
    fn = upload(seed=906)
    write_meta(client, fn, regions=[box(class_name="face", region_name="face", confirmed=True,
                                        cx=.5, cy=.25, w=.12, h=.15)])
    db = app._db()
    if not [t for t, _ in _tables_about_files(db) if t == "face_regions"]:
        pytest.skip("people module not loaded")
    db.execute("DELETE FROM face_regions WHERE rel_path=?", (fn,))
    db.execute("INSERT INTO face_regions(rel_path,cx,cy,w,h,name,confirmed,cluster_id) "
               "VALUES(?,?,?,?,?,?,?,?)", (fn, .5, .25, .12, .15, "", 0, 4243))
    db.commit()
    _reindex(app, fn)
    row = db.execute("SELECT name FROM face_regions WHERE rel_path=?", (fn,)).fetchone()
    assert (row[0] or "") == "", f"class label became a person name: {row[0]!r}"