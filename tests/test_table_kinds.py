"""! @file
@brief Table kinds: add_table(kind=), the missing-kind warning, host.table_kinds()
and a source check that every add_table call in the tree declares its kind.
"""
import ast
import glob
import os

from modules.host import Host, TABLE_KINDS, table_names

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE_TABLES = {"files": "mirrored", "file_history": "state", "albums": "state",
               "album_members": "mirrored", "raws": "state", "upload_queue": "state",
               "user_prefs": "state", "cim_meta": "state"}


class _Log:
    """! @brief A logger that keeps its warnings."""
    def __init__(self):
        self.warnings = []

    def warning(self, msg):
        self.warnings.append(msg)

    def error(self, msg):
        self.warnings.append(msg)


def _bare_host():
    return Host(app=None, db=None, config={}, logger=_Log(), thread_manager=None,
                media_dir="media", safe_path=None, save_config=None)


def test_table_names_parse_ddl():
    ddl = """
        -- CREATE TABLE IF NOT EXISTS in_a_comment (x)
        CREATE TABLE IF NOT EXISTS one (a INTEGER);
        CREATE INDEX IF NOT EXISTS idx_one ON one(a);
        create table "two" (b);
    """
    assert table_names(ddl) == ["one", "two"]


def test_add_table_without_kind_warns_and_is_state():
    h = _bare_host()
    h._current_module = "xmod"
    h.add_table("CREATE TABLE IF NOT EXISTS t_nokind (x)")
    assert any("xmod" in w and "t_nokind" in w for w in h.logger.warnings)
    assert h.table_kinds()["t_nokind"] == {"kind": "state", "module_id": "xmod"}
    h.add_table("CREATE TABLE IF NOT EXISTS t_typo (x)", kind="cachee")
    assert h.table_kinds()["t_typo"]["kind"] == "state" and len(h.logger.warnings) == 2


def test_add_table_with_kind():
    h = _bare_host()
    h._current_module = "ymod"
    h.add_table("CREATE TABLE IF NOT EXISTS a (x); CREATE TABLE IF NOT EXISTS b (y)", kind="cache")
    h.declare_table("c", "mirrored")
    assert not h.logger.warnings
    assert h.table_kinds() == {"a": {"kind": "cache", "module_id": "ymod"},
                               "b": {"kind": "cache", "module_id": "ymod"},
                               "c": {"kind": "mirrored", "module_id": "core"}}


def test_table_kinds_cover_core_and_modules(host):
    kinds = host.table_kinds()
    for name, kind in CORE_TABLES.items():
        assert kinds[name]["kind"] == kind, name
    for t in host.db_tables:
        assert t["kind"] in TABLE_KINDS
        for name in table_names(t["ddl"]):
            # a shared DDL (import_sources) may be added by several modules
            assert kinds[name]["kind"] == t["kind"], name
    assert all(v["kind"] in TABLE_KINDS for v in kinds.values())
    # a few module tables, when their module is loaded
    expect = {"image_embeddings": "cache", "metadata_index": "cache", "ratings": "mirrored",
              "favorites": "state", "auth_api_keys": "state"}
    for name, kind in expect.items():
        if name in kinds:
            assert kinds[name]["kind"] == kind, name


def test_every_live_table_has_a_kind(app, host):
    kinds = host.table_kinds()
    live = {r[0] for r in app._db().execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    # modules that create tables themselves (not through add_table)
    outside = {"training_presets", "books"}
    missing = sorted(n for n in live - set(kinds)
                     if not n.startswith(("sqlite_", "book_")) and n not in outside)
    assert not missing, f"tables without a kind: {missing}"


def test_every_add_table_call_declares_kind():
    bad = []
    for path in glob.glob(os.path.join(ROOT, "modules", "**", "*.py"), recursive=True) + \
            [os.path.join(ROOT, "manager.py")]:
        if os.sep + "tests" + os.sep in path or path.endswith(os.path.join("modules", "host.py")):
            continue
        with open(path, encoding="utf-8") as f:
            src = f.read()
        if "add_table(" not in src:
            continue
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "add_table":
                kw = {k.arg: k.value for k in node.keywords}
                val = kw.get("kind")
                if not (isinstance(val, ast.Constant) and val.value in TABLE_KINDS):
                    bad.append(f"{os.path.relpath(path, ROOT)}:{node.lineno}")
    assert not bad, f"add_table without a literal kind: {bad}"
