"""File edit changelog (record / undo / redo / ImageHistory text) + upsert,
against manager's live implementation (`_history_*`, `_upsert_file`)."""
import pytest


def _run_history(rec, entries, undo, redo, as_text, rel):
    rec(rel, "description", "", "one")
    rec(rel, "description", "one", "one")          # no-op edit ignored
    rec(rel, "description", "one", "two")
    assert [e["new"] for e in entries(rel)] == ["one", "two"]
    u = undo(rel)
    assert u["old"] == "one" and u["new"] == "two"
    assert [e["new"] for e in entries(rel)] == ["one"]
    assert len(entries(rel, True)) == 2
    assert redo(rel)["new"] == "two"
    assert redo(rel) is None
    undo(rel)
    rec(rel, "description", "one", "three")         # new edit clears the redo tail
    assert redo(rel) is None
    assert [e["new"] for e in entries(rel)] == ["one", "three"]
    txt = as_text(rel)
    assert "three" in txt and txt.count("\n") == 1


def test_manager_history(app):
    rel = "hist/x.jxl"
    db = app._db()
    db.execute("DELETE FROM file_history WHERE rel_path=?", (rel,)); db.commit()
    _run_history(app._history_record, app._history_entries, app._history_undo,
                 app._history_redo, app._history_as_imagehistory, rel)
    db.execute("DELETE FROM file_history WHERE rel_path=?", (rel,)); db.commit()


def test_manager_upsert_file_conflict_updates(app):
    rel = "hist/up.jxl"
    app._upsert_file(rel, 1.0, 10, 20, "sha", b"", b"", ["a"], "d1")
    app._upsert_file(rel, 2.0, 30, 40, "sha", b"", b"", ["b"], "d2")
    row = app._get_file_row(rel)
    assert row["width"] == 30 and row["description"] == "d2"
    app._purge_file_everywhere(rel)
    assert app._get_file_row(rel) is None

