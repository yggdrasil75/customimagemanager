"""! @file
@brief Library pipeline: organise the whole library - automatic albums from trips,
events, places, people and look-alikes, judged and named by the LLM, then tags,
folders and a per-photo pipeline run that knows what the group is.

The per-photo pipeline (modules/pipeline) looks at one image at a time. This
one runs over the library and uses what only the library knows - taken dates
and GPS across files, the map module's place names, the people module's named
faces, the embedding module's vectors, the albums that already exist - to:

  * group the files (engine.py nodes over clustering.py),
  * keep the groups that fit a rule and name them (the `llm` service, with a
    contact sheet of each group; without an LLM the groups are named from a
    template of their place and dates),
  * plan actions: create or update albums, tag the members, move them into
    folders, and run the per-photo pipeline on them with the group as context
    ("part of 'Lisbon, June 2024', with Ana and Rui").

A run only plans. The plan is a preview the admin applies entry by entry (or
all at once); in automatic mode the library is re-planned on an interval while
the server is idle and, with "apply", the plan is applied straight away (moves
only when Settings allow them). Albums the pipeline made are remembered in
`library_pipeline_albums`, so a re-run updates them instead of making copies,
and a group that is already mostly one of the user's albums is left alone.

The graph is edited in Settings -> Pipeline (Library), on the same node editor
as the per-photo pipeline; it is the `library_pipeline_graph` setting.
"""

import copy
import io
import json
import os
import threading
import time

import numpy as np
from flask import jsonify, request
from PIL import Image

from . import clustering as cl
from . import engine
from .library import Library

MANIFEST = {
    "id":          "library_pipeline",
    "name":        "Library pipeline (automatic albums)",
    "version":     "1.0.0",
    "description": "Organises the whole library: groups trips, events, places, people and look-alikes, "
                   "lets the LLM judge and name them, and creates albums, tags, folders and per-photo "
                   "pipeline runs that know the group - previewed, or automatic on a schedule.",
    "core":        False,
    "requires":    ["pipeline"],
    "pip":         [],
    "assets":      ["library_pipeline.js", "library_pipeline.css"],
}

FEATURE = "settings.pipeline"
AUTO_MODES = [{"value": "off", "label": "Off"},
              {"value": "preview", "label": "Plan only (review and apply by hand)"},
              {"value": "apply", "label": "Plan and apply"}]
IDLE_SECONDS = 300
TICK_SECONDS = 60
CELL = 256
MAX_LLM_FAILS = 2
UPDATE_SHARE = 0.5        # an auto album holding this share of a group is that group's album

DDL = """
CREATE TABLE IF NOT EXISTS library_pipeline_albums (
    album    TEXT PRIMARY KEY,
    group_id TEXT NOT NULL DEFAULT '',
    created  REAL,
    updated  REAL
);
CREATE TABLE IF NOT EXISTS library_pipeline_runs (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    started  REAL NOT NULL,
    finished REAL,
    mode     TEXT NOT NULL DEFAULT 'preview',
    status   TEXT NOT NULL DEFAULT 'running',
    plan     TEXT NOT NULL DEFAULT '[]',
    log      TEXT NOT NULL DEFAULT '[]',
    applied  TEXT NOT NULL DEFAULT '{}'
);
"""


def _clean_graph(v):
    if isinstance(v, str):
        v = json.loads(v)
    if not engine.is_graph(v):
        raise ValueError("not a library/1 graph")
    return v


def _clean_mode(v):
    v = str(v or "off")
    if v not in {m["value"] for m in AUTO_MODES}:
        raise ValueError("unknown mode %r" % v)
    return v


def collage(host, rels, cols=3):
    """! @brief A contact sheet (BGR array) of the files' thumbnails, None when none decode."""
    tiles = []
    for rel in rels:
        abs_p = host.safe_path(host.media_dir, rel)
        if not abs_p or not os.path.exists(abs_p):
            continue
        im = None
        try:
            got = host.core.thumb_bytes(rel, abs_p)
            if got:
                im = Image.open(io.BytesIO(got[0])).convert("RGB")
        except Exception:
            im = None
        if im is None:
            try:
                arr = host.core.read_image(abs_p)
                if arr is not None:
                    im = Image.fromarray(np.asarray(arr)[..., :3].astype(np.uint8)).convert("RGB")
            except Exception:
                im = None
        if im is not None:
            im.thumbnail((CELL, CELL))
            tiles.append(im)
    if not tiles:
        return None
    cols = min(cols, len(tiles))
    rows = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * CELL, rows * CELL), (0, 0, 0))
    for i, im in enumerate(tiles):
        x, y = (i % cols) * CELL, (i // cols) * CELL
        sheet.paste(im, (x + (CELL - im.width) // 2, y + (CELL - im.height) // 2))
    return np.asarray(sheet)[:, :, ::-1].copy()


class Runner:
    """! @brief One run at a time (plan, then optionally apply), in a background thread."""

    def __init__(self, host):
        self.host = host
        self.lock = threading.Lock()
        self.state = {"running": False, "run_id": None, "phase": "", "progress": "", "cancel": False}

    # -- helpers -----------------------------------------------------------------
    def busy(self):
        return self.state["running"]

    def _llm(self):
        """! @brief The LLM call for a run, or None when none is configured. After
        MAX_LLM_FAILS failures in a row it stops trying for the rest of the run
        (an unreachable endpoint shouldn't cost a timeout per group)."""
        svc = self.host.get_service("llm")
        cfg = self.host.config
        if not svc or not (cfg.get("oai_endpoint") and cfg.get("oai_model")):
            return None
        fails = {"n": 0}

        def call(prompt, img, want):
            if fails["n"] >= MAX_LLM_FAILS:
                raise RuntimeError("LLM unreachable, skipped")
            try:
                out = svc["call"](prompt, img, want)
            except Exception:
                fails["n"] += 1
                raise
            fails["n"] = 0
            return out
        return call

    def _progress(self, msg):
        if self.state["cancel"]:
            raise engine.Cancelled("cancelled")
        self.state["progress"] = msg
        self.host.set_status("Library pipeline: %s" % msg)

    def auto_albums(self, db):
        return {r["group_id"]: r["album"] for r in db.execute(
            "SELECT album, group_id FROM library_pipeline_albums").fetchall()}

    # -- start -------------------------------------------------------------------------
    def start(self, mode="preview", graph=None):
        """! @brief Plan (and with mode "apply", apply) in the background. @return run id or None if busy."""
        with self.lock:
            if self.state["running"]:
                return None
            db = self.host.db()
            cur = db.execute("INSERT INTO library_pipeline_runs(started, mode, status) VALUES (?,?,'running')",
                             (time.time(), mode))
            db.commit()
            rid = cur.lastrowid
            self.state.update(running=True, run_id=rid, phase="planning", progress="", cancel=False)
        threading.Thread(target=self._run, args=(rid, mode, graph), daemon=True,
                         name="library-pipeline").start()
        return rid

    def apply_async(self, rid, ids):
        with self.lock:
            if self.state["running"]:
                return False
            self.state.update(running=True, run_id=rid, phase="applying", progress="", cancel=False)
        threading.Thread(target=self._apply_thread, args=(rid, ids), daemon=True,
                         name="library-pipeline-apply").start()
        return True

    def _run(self, rid, mode, graph):
        db = self.host.db()
        log, plan, status = [], [], "planned"
        try:
            saved = self.host.config.get("library_pipeline_graph") or engine.DEFAULT_GRAPH
            g = copy.deepcopy(graph or saved)
            ctx = engine.Context(Library(self.host), llm=self._llm(),
                                 collage=lambda rels: collage(self.host, rels),
                                 report=self._progress, auto_albums=self.auto_albums(db))
            out = engine.run_graph(g, ctx)
            plan, log = out["plan"], out["log"]
            if graph is None:
                self._keep_compiled_rules(saved, g)
            if not self.host.config.get("library_pipeline_allow_moves"):
                for e in plan:
                    if e["op"] == "move":
                        e["blocked"] = "moving files is off in Settings -> Modules (Library pipeline)"
            self._save(db, rid, status="planned", plan=plan, log=log)
            if mode == "apply":
                self.state["phase"] = "applying"
                self._apply(rid, [e["id"] for e in plan if not e.get("blocked")])
            status = "done" if mode == "apply" else "planned"
        except Exception as e:
            status = "cancelled" if isinstance(e, engine.Cancelled) else "failed"
            log.append("%s: %s" % (status, e))
            self.host.logger.error("library pipeline run %s %s: %s" % (rid, status, e))
        finally:
            self._save(db, rid, status=status, log=log, finished=time.time())
            self.state.update(running=False, phase="", progress="")
            self.host.set_status("Ready.")

    def _keep_compiled_rules(self, saved, ran):
        """! @brief A group_rule node compiled its rule this run: save it so the next run is the same."""
        compiled = {n["id"]: n.get("compiled") for n in ran.get("nodes", []) if n.get("type") == "group_rule"}
        changed = False
        g = copy.deepcopy(saved)
        for n in g.get("nodes", []):
            if n.get("id") in compiled and compiled[n["id"]] and n.get("compiled") != compiled[n["id"]]:
                n["compiled"] = compiled[n["id"]]
                changed = True
        if changed:
            self.host.set_config("library_pipeline_graph", g)

    def _save(self, db, rid, **cols):
        sets, args = [], []
        for k, v in cols.items():
            sets.append("%s=?" % k)
            args.append(json.dumps(v) if isinstance(v, (list, dict)) else v)
        db.execute("UPDATE library_pipeline_runs SET %s WHERE id=?" % ", ".join(sets), args + [rid])
        db.commit()

    def run_row(self, rid=None):
        db = self.host.db()
        r = (db.execute("SELECT * FROM library_pipeline_runs WHERE id=?", (rid,)).fetchone() if rid
             else db.execute("SELECT * FROM library_pipeline_runs ORDER BY id DESC LIMIT 1").fetchone())
        if not r:
            return None
        out = dict(r)
        for k in ("plan", "log", "applied"):
            try:
                out[k] = json.loads(out[k] or ("{}" if k == "applied" else "[]"))
            except ValueError:
                out[k] = {} if k == "applied" else []
        return out

    # -- apply -------------------------------------------------------------------------
    def _apply_thread(self, rid, ids):
        try:
            self._apply(rid, ids)
        except Exception as e:
            self.host.logger.error("library pipeline apply %s: %s" % (rid, e))
        finally:
            self.state.update(running=False, phase="", progress="")
            self.host.set_status("Ready.")

    def _apply(self, rid, ids):
        row = self.run_row(rid)
        if not row:
            return
        db = self.host.db()
        applied = row["applied"]
        want = set(int(i) for i in ids)
        for e in row["plan"]:
            if e["id"] not in want or str(e["id"]) in applied or e.get("blocked"):
                continue
            self._progress("%s %s" % (e["op"], e["name"]))
            try:
                res = APPLY[e["op"]](self, e)
            except engine.Cancelled:
                raise
            except Exception as ex:
                res = {"ok": False, "error": str(ex)}
            applied[str(e["id"])] = res
            self._save(db, rid, applied=applied)

    def apply_album(self, e):
        core, db = self.host.core, self.host.db()
        files = [f for f in e["files"] if db.execute("SELECT 1 FROM files WHERE rel_path=?", (f,)).fetchone()]
        if not files:
            return {"ok": False, "error": "no files left"}
        auto = {r["album"]: r["group_id"] for r in db.execute("SELECT album, group_id FROM library_pipeline_albums")}
        members = Library(self.host).album_members()
        target = None
        if e.get("update_existing", True):
            best, share = cl.album_overlap(files, {a: m for a, m in members.items() if a in auto})
            if best and share >= UPDATE_SHARE:
                target = best
        created = False
        if target is None:
            name, k = e["album"], 2
            existing = Library(self.host).album_names()
            while name in existing and name not in auto:
                name = "%s (%d)" % (e["album"], k)
                k += 1
            target = name
            created = core.album_create(target, e.get("description", "") if e.get("set_description") else "",
                                        owner_hint=files[0])
        added = core.album_add(files, target)
        if e.get("set_description") and e.get("description"):
            row = db.execute("SELECT description FROM albums WHERE name=?", (target,)).fetchone()
            if row is not None and (not row["description"] or target in auto):
                core.album_set_description(target, e["description"])
        if created and e.get("cover") in files:
            core.album_set_cover(target, e["cover"])
        now = time.time()
        db.execute("INSERT INTO library_pipeline_albums(album, group_id, created, updated) VALUES (?,?,?,?) "
                   "ON CONFLICT(album) DO UPDATE SET group_id=excluded.group_id, updated=excluded.updated",
                   (target, e["group"], now, now))
        db.commit()
        return {"ok": True, "album": target, "created": bool(created), "added": added}

    def apply_tag(self, e):
        n = 0
        for rel in e["files"]:
            try:
                self.host.update_file(rel, add={"tags": ["?" + e["tag"]]})
                n += 1
            except Exception as ex:
                self.host.logger.warning("library pipeline tag %s: %s" % (rel, ex))
        return {"ok": True, "tagged": n}

    def apply_move(self, e):
        if not self.host.config.get("library_pipeline_allow_moves"):
            return {"ok": False, "error": "moving files is off"}
        own = self.host.get_service("ownership")
        folder = engine.safe_folder(e["folder"])
        moved, errors = 0, []
        for rel in e["files"]:
            owner = own["owner_of"](rel) if own else None
            base = own["personal_folder"](owner, folder) if (own and owner) else folder
            new_rel = "%s/%s" % (base.strip("/"), rel.rsplit("/", 1)[-1])
            if new_rel == rel:
                continue
            ok, err = self.host.core.move_file(rel, new_rel)
            if ok:
                moved += 1
            else:
                errors.append("%s: %s" % (rel, err))
        return {"ok": not errors, "moved": moved, "errors": errors[:20]}

    def apply_photo(self, e):
        svc = self.host.get_service("pipeline")
        if not svc:
            return {"ok": False, "error": "the per-photo pipeline module is off"}
        core, db = self.host.core, self.host.db()
        lib = Library(self.host)
        done, skipped, errors = 0, 0, 0
        for i, rel in enumerate(e["files"]):
            row = db.execute("SELECT description, media_kind FROM files WHERE rel_path=?", (rel,)).fetchone()
            if not row or (row["media_kind"] or "image") != "image" or (e.get("only_undescribed") and (row["description"] or "").strip()):
                skipped += 1
                continue
            abs_p = self.host.safe_path(self.host.media_dir, rel)
            try:
                img = core.read_image(abs_p)
                if img is None:
                    errors += 1
                    continue
                it = lib.item(rel) or {}
                self._progress("photo pipeline %d/%d (%s)" % (i + 1, len(e["files"]), e["name"]))
                analysis = svc["run"](core.to_bgr(img), abs_p, None, None,
                                      known_extra={"context": e.get("context", ""), "people": it.get("people")})
                svc["apply"](abs_p, analysis)
                done += 1
            except engine.Cancelled:
                raise
            except Exception as ex:
                errors += 1
                self.host.logger.warning("library pipeline photo %s: %s" % (rel, ex))
        return {"ok": errors == 0, "done": done, "skipped": skipped, "errors": errors}


APPLY = {"album": Runner.apply_album, "tag": Runner.apply_tag, "move": Runner.apply_move,
         "photo": Runner.apply_photo}


def register(host):
    """! @brief Settings, tables, routes, the automatic schedule and the `library_pipeline` service."""
    host.add_table(DDL, kind="state")   # albums it made (so re-runs update them) and its runs
    host.add_config_key("library_pipeline_graph", default=engine.DEFAULT_GRAPH, validate=_clean_graph,
                        tab="pipeline")
    host.add_config_key("library_pipeline_auto", default="off", validate=_clean_mode)
    host.add_config_key("library_pipeline_interval_h", default=24,
                        validate=lambda v: max(1, min(24 * 30, int(float(v)))))
    host.add_config_key("library_pipeline_allow_moves", default=False, validate=bool)
    host.add_settings_field(key="library_pipeline_auto", label="Run automatically", kind="select",
                            options=AUTO_MODES, pane="module",
                            help="Re-plans the library on the interval while the server is idle.")
    host.add_settings_field(key="library_pipeline_interval_h", label="Every (hours)", kind="number",
                            pane="module")
    host.add_settings_field(key="library_pipeline_allow_moves", label="Allow moving files into folders",
                            kind="toggle", pane="module",
                            help="Off: 'Move into folders' entries are planned but never applied.")
    runner = Runner(host)

    def api_graph():
        g = host.config.get("library_pipeline_graph") or engine.DEFAULT_GRAPH
        return jsonify({"success": True, "graph": g, "catalog": engine.catalog(),
                        "default": engine.DEFAULT_GRAPH})

    def api_run():
        d = request.get_json(silent=True) or {}
        mode = "apply" if d.get("mode") == "apply" else "preview"
        graph = d.get("graph")
        if graph is not None and not engine.is_graph(graph):
            return jsonify({"success": False, "error": "not a library/1 graph"}), 400
        rid = runner.start(mode, graph)
        if rid is None:
            return jsonify({"success": False, "error": "a run is already going"}), 409
        return jsonify({"success": True, "run_id": rid})

    def api_apply():
        d = request.get_json(silent=True) or {}
        row = runner.run_row(d.get("run_id"))
        if not row or row["status"] not in ("planned", "done"):
            return jsonify({"success": False, "error": "no finished plan to apply"}), 400
        ids = d.get("ids")
        if ids is None:
            ids = [e["id"] for e in row["plan"]]
        if not runner.apply_async(row["id"], ids):
            return jsonify({"success": False, "error": "a run is already going"}), 409
        return jsonify({"success": True, "run_id": row["id"]})

    def api_cancel():
        runner.state["cancel"] = True
        return jsonify({"success": True})

    def api_status():
        rid = request.args.get("run_id")
        row = runner.run_row(int(rid) if rid and rid.isdigit() else None)
        st = {k: runner.state[k] for k in ("running", "run_id", "phase", "progress")}
        return jsonify({"success": True, "state": st, "run": row,
                        "auto": host.config.get("library_pipeline_auto", "off"),
                        "allow_moves": bool(host.config.get("library_pipeline_allow_moves")),
                        "llm": runner._llm() is not None})

    host.add_route("/api/library_pipeline/graph", api_graph, feature=FEATURE)
    host.add_route("/api/library_pipeline/status", api_status, feature=FEATURE)
    host.add_route("/api/library_pipeline/run", api_run, methods=["POST"], feature=FEATURE, level="write")
    host.add_route("/api/library_pipeline/apply", api_apply, methods=["POST"], feature=FEATURE, level="write")
    host.add_route("/api/library_pipeline/cancel", api_cancel, methods=["POST"], feature=FEATURE, level="write")

    # -- automatic runs -----------------------------------------------------------------
    def due():
        mode = host.config.get("library_pipeline_auto", "off")
        if mode == "off" or runner.busy():
            return None
        if time.time() - (host.core.last_activity() or 0) < IDLE_SECONDS:
            return None
        last = runner.run_row()
        every = float(host.config.get("library_pipeline_interval_h", 24)) * 3600
        if last and time.time() - (last.get("finished") or last["started"]) < every:
            return None
        return mode

    def loop():
        while True:
            time.sleep(TICK_SECONDS)
            try:
                mode = due()
                if mode:
                    runner.start(mode)
            except Exception as e:
                host.logger.error("library pipeline schedule: %s" % e)

    host.on_startup(lambda: threading.Thread(target=loop, daemon=True, name="library-pipeline-auto").start())
    host.add_asset("library_pipeline.js")
    host.add_asset("library_pipeline.css", kind="css")
    host.provide_service("library_pipeline", {"run": runner.start, "status": lambda: dict(runner.state),
                                               "last_run": runner.run_row})
    host.logger.info("library pipeline registered")