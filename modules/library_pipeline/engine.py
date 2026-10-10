"""! @file
@brief The library pipeline's dataflow runner (schema library/1).

The per-photo pipeline (modules/pipeline, graph/1) sees one image. This one sees
the library: its values are sets of files and groups of files, and its nodes
select, group (trips, events, places, people, look-alikes), judge and name
groups (with the LLM when one is configured) and finally plan actions on them
(create / update albums, tag, move, run the per-photo pipeline with what the
group says about its photos). Same wire format as graph/1, so the same editor
draws it:

    {"schema": "library/1", "nodes": [
        {"id": "start", "type": "start", "q": "", "ui": {...}},
        {"id": "trips", "type": "group_trips", "gap_hours": 36,
         "in": {"files": ["start", "files"]}},
        {"id": "albums", "type": "make_albums", "in": {"groups": ["trips", "groups"]}},
        {"id": "end", "type": "end", "in": {"plan": [["albums", "plan"]]}}]}

A run only PLANS: run_graph returns {"plan": [entries], "log": [...]}; the
module shows the plan as a preview and applies the entries the user keeps (or,
in automatic mode, all of them). Every entry carries its files, a title and the
group's facts, so a preview reads like the result.
"""

import datetime
import json
import re

from . import clustering as cl

SCHEMA = "library/1"
P = lambda name, type_, multi=False, opt=False: {"name": name, "type": type_, "multi": multi, "optional": opt}
MAX_SAMPLES = 9

CATALOG = {
    "start": {"label": "Library", "kind": "io", "inputs": [],
              "outputs": [P("files", "files")],
              "params": {"q": "text", "folder": "text", "album": "text", "use_file_dates": "bool"},
              "help": "The pictures and videos to work on: every one, or what a gallery search (q), "
                      "folder or album lists. File times only count as taken dates with use_file_dates."},
    "end": {"label": "End (the plan)", "kind": "io", "outputs": [],
            "inputs": [P("plan", "plan", multi=True, opt=True)]},
    # -- select --------------------------------------------------------------------
    "query": {"label": "Search", "kind": "select",
              "inputs": [P("run", "bool", opt=True)], "outputs": [P("files", "files")],
              "params": {"q": "text", "folder": "text", "album": "text"},
              "help": "Files a gallery search lists (any search syntax: tag:, person:, date:, near:, ...)."},
    "filter": {"label": "Filter files", "kind": "select",
               "inputs": [P("files", "files")], "outputs": [P("pass", "files"), P("rest", "files")],
               "params": {"kind": ["any", "image", "video"], "has_date": ["any", "yes", "no"],
                          "has_gps": ["any", "yes", "no"], "in_album": ["any", "yes", "no"],
                          "min_rating": "number", "person": "text", "tag": "text", "folder": "text"},
               "help": "pass = the files matching every set condition, rest = the others."},
    "union": {"label": "Combine files", "kind": "select",
              "inputs": [P("files", "files", multi=True)], "outputs": [P("files", "files")]},
    # -- group ----------------------------------------------------------------------
    "group_trips": {"label": "Group: trips", "kind": "group",
                    "inputs": [P("files", "files"), P("run", "bool", opt=True)], "outputs": [P("groups", "groups")],
                    "params": {"gap_hours": "number", "min_files": "number", "max_km": "number",
                               "home": "text", "home_km": "number"},
                    "help": "Photos taken close together in time (no gap over gap_hours), away from home "
                            "(home = 'lat, lon' and home_km). max_km > 0 also splits at a jump that far."},
    "group_events": {"label": "Group: events", "kind": "group",
                     "inputs": [P("files", "files"), P("run", "bool", opt=True)], "outputs": [P("groups", "groups")],
                     "params": {"gap_minutes": "number", "min_files": "number"},
                     "help": "Bursts of photos with no pause longer than gap_minutes (a party, a game)."},
    "group_places": {"label": "Group: places", "kind": "group",
                     "inputs": [P("files", "files"), P("run", "bool", opt=True)], "outputs": [P("groups", "groups")],
                     "params": {"level": ["city", "admin1", "country"], "per_year": "bool", "min_files": "number"},
                     "help": "One group per place (needs the map module's places), optionally per year."},
    "group_people": {"label": "Group: people", "kind": "group",
                     "inputs": [P("files", "files"), P("run", "bool", opt=True)], "outputs": [P("groups", "groups")],
                     "params": {"mode": ["each", "same_set"], "min_files": "number", "min_people": "number"},
                     "help": "each: one group per named person; same_set: one per exact set of people."},
    "group_similar": {"label": "Group: look-alikes", "kind": "group",
                      "inputs": [P("files", "files"), P("run", "bool", opt=True)], "outputs": [P("groups", "groups")],
                      "params": {"threshold": "number", "min_files": "number", "window_hours": "number"},
                      "help": "Image embeddings at least `threshold` alike (cosine). window_hours > 0 only links "
                              "photos taken that close together. Needs the embedding module's vectors."},
    "group_rule": {"label": "Group: AI rule", "kind": "ai",
                   "inputs": [P("files", "files"), P("run", "bool", opt=True)],
                   "outputs": [P("groups", "groups"), P("rule", "text")],
                   "params": {"rule": "textarea", "compiled": "textarea"},
                   "help": "Describe the albums you want ('similar location, similar time frame, likely the same "
                           "trip'). The LLM turns the rule into a grouping method with its settings and a question "
                           "each group is checked against; the result is kept in `compiled` (clear it to recompile)."},
    # -- refine -----------------------------------------------------------------------
    "merge_groups": {"label": "Merge alike neighbours", "kind": "refine",
                     "inputs": [P("groups", "groups")], "outputs": [P("groups", "groups")],
                     "params": {"threshold": "number", "max_gap_hours": "number"},
                     "help": "Join consecutive groups whose embeddings look alike and are at most max_gap_hours "
                             "apart (one trip split by a quiet day)."},
    "filter_groups": {"label": "Filter groups", "kind": "refine",
                      "inputs": [P("groups", "groups")], "outputs": [P("kept", "groups"), P("dropped", "groups")],
                      "params": {"min_files": "number", "max_files": "number", "min_days": "number",
                                 "max_days": "number", "min_gps": "number", "min_people": "number",
                                 "skip_in_album": "number"},
                      "help": "skip_in_album (0..1): drop a group when that share of it is already in one album."},
    "union_groups": {"label": "Combine groups", "kind": "refine",
                     "inputs": [P("groups", "groups", multi=True)], "outputs": [P("groups", "groups")]},
    "ai_judge": {"label": "AI: keep groups matching a rule", "kind": "ai",
                 "inputs": [P("groups", "groups"), P("run", "bool", opt=True)],
                 "outputs": [P("kept", "groups"), P("rejected", "groups")],
                 "params": {"rule": "textarea", "samples": "number"},
                 "help": "The LLM sees each group's facts (dates, places, people, tags) and a contact sheet of "
                         "`samples` photos and answers whether it fits the rule; it also names the group. "
                         "Without an LLM every group is kept."},
    "ai_name": {"label": "AI: name and describe", "kind": "ai",
                "inputs": [P("groups", "groups"), P("run", "bool", opt=True)], "outputs": [P("groups", "groups")],
                "params": {"prompt": "textarea", "samples": "number", "template": "text", "overwrite": "bool"},
                "help": "A short album title and a one-sentence description from the group's facts and photos. "
                        "Without an LLM the template names it."},
    "name_template": {"label": "Name from template", "kind": "refine",
                      "inputs": [P("groups", "groups")], "outputs": [P("groups", "groups")],
                      "params": {"template": "text", "overwrite": "bool"},
                      "help": "{place} {city} {region} {country} {range} {start} {end} {year} {month} "
                              "{people} {count} {method} {name}"},
    # -- act (plan) --------------------------------------------------------------------
    "make_albums": {"label": "Albums", "kind": "act",
                    "inputs": [P("groups", "groups")], "outputs": [P("plan", "plan")],
                    "params": {"template": "text", "cover": ["best", "first", "middle"],
                               "update_existing": "bool", "description": "bool"},
                    "help": "One album per group, named by the template (default {name}). An album this pipeline "
                            "made before for mostly the same photos is updated instead of duplicated."},
    "tag_groups": {"label": "Tag members", "kind": "act",
                   "inputs": [P("groups", "groups")], "outputs": [P("plan", "plan")],
                   "params": {"template": "text"},
                   "help": "Adds the tag (template, e.g. trip/{name}) to every photo of the group, unconfirmed."},
    "move_groups": {"label": "Move into folders", "kind": "act",
                    "inputs": [P("groups", "groups")], "outputs": [P("plan", "plan")],
                    "params": {"template": "text"},
                    "help": "Moves every photo of the group into the folder (template, e.g. {year}/{name}), "
                            "keeping its file name. Personal libraries stay inside their own folder."},
    "photo_pipeline": {"label": "Per-photo pipeline with group context", "kind": "act",
                       "inputs": [P("groups", "groups", opt=True), P("files", "files", opt=True)],
                       "outputs": [P("plan", "plan")],
                       "params": {"only_undescribed": "bool", "context": "bool"},
                       "help": "Runs the per-photo Smart Tag pipeline on the members, telling it what the library "
                               "knows: the group's name, dates, places and the people in it."},
}

DEFAULT_GRAPH = {"schema": SCHEMA, "nodes": [
    {"id": "start", "type": "start", "ui": {"x": 20, "y": 40}},
    {"id": "dated", "type": "filter", "label": "Has a taken date", "has_date": "yes",
     "in": {"files": ["start", "files"]}, "ui": {"x": 280, "y": 40}},
    {"id": "trips", "type": "group_trips", "label": "Trips", "gap_hours": 36, "min_files": 8,
     "in": {"files": ["dated", "pass"]}, "ui": {"x": 540, "y": 40}},
    {"id": "merge", "type": "merge_groups", "label": "Merge split trips", "threshold": 0.8, "max_gap_hours": 48,
     "in": {"groups": ["trips", "groups"]}, "ui": {"x": 800, "y": 40}},
    {"id": "fresh", "type": "filter_groups", "label": "Not already an album", "skip_in_album": 0.8,
     "in": {"groups": ["merge", "groups"]}, "ui": {"x": 1060, "y": 40}},
    {"id": "judge", "type": "ai_judge", "label": "Likely one trip?",
     "rule": "Keep the group when the photos look like one trip or outing: similar locations, a continuous "
             "time frame, the same people or scenery. Reject everyday photos at home.",
     "samples": 6, "in": {"groups": ["fresh", "kept"]}, "ui": {"x": 1320, "y": 40}},
    {"id": "name", "type": "ai_name", "label": "Name it", "samples": 6, "template": "{place} {range}",
     "in": {"groups": ["judge", "kept"]}, "ui": {"x": 1580, "y": 40}},
    {"id": "albums", "type": "make_albums", "label": "Make albums", "template": "{name}", "cover": "best",
     "update_existing": True, "description": True, "in": {"groups": ["name", "groups"]}, "ui": {"x": 1840, "y": 40}},
    {"id": "end", "type": "end", "in": {"plan": [["albums", "plan"]]}, "ui": {"x": 2100, "y": 40}},
]}


class Cancelled(Exception):
    """! @brief Raised by the context's report() to stop a run; never swallowed by a node."""


def catalog():
    return {k: dict(v) for k, v in CATALOG.items()}


def is_graph(g):
    return isinstance(g, dict) and g.get("schema") == SCHEMA


def _num(n, key, default):
    try:
        v = n.get(key)
        return default if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return default


def _flag(n, key, default=False):
    v = n.get(key)
    if v in (None, ""):
        return default
    if isinstance(v, str):
        return v.strip().lower() in ("1", "yes", "true", "on")
    return bool(v)


def _truthy(v):
    if isinstance(v, str):
        return v.strip().lower() not in ("", "no", "false", "0", "none", "null")
    return bool(v)


# -- naming ---------------------------------------------------------------------------
_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December"]


def _date(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc) if t is not None else None


def date_range(start, end):
    """! @brief 'Jun 3 - 9, 2024', 'Jun 28 - Jul 2, 2024', 'Dec 30, 2023 - Jan 2, 2024'."""
    a, b = _date(start), _date(end)
    if not a:
        return ""
    m = lambda d: _MONTHS[d.month - 1][:3]
    if not b or a.date() == b.date():
        return "%s %d, %d" % (m(a), a.day, a.year)
    if a.year != b.year:
        return "%s %d, %d - %s %d, %d" % (m(a), a.day, a.year, m(b), b.day, b.year)
    if a.month != b.month:
        return "%s %d - %s %d, %d" % (m(a), a.day, m(b), b.day, a.year)
    return "%s %d - %d, %d" % (m(a), a.day, b.day, a.year)


def fields(group, st):
    """! @brief The template placeholders of a group."""
    a, b = _date(st.get("start")), _date(st.get("end"))
    place = (st.get("cities") or st.get("regions") or st.get("countries") or [""])[0]
    return {"name": group.get("name") or "", "place": place,
            "city": (st.get("cities") or [""])[0], "region": (st.get("regions") or [""])[0],
            "country": (st.get("countries") or [""])[0],
            "range": date_range(st.get("start"), st.get("end")),
            "start": a.strftime("%Y-%m-%d") if a else "", "end": b.strftime("%Y-%m-%d") if b else "",
            "year": str(a.year) if a else "", "month": _MONTHS[a.month - 1] if a else "",
            "people": " & ".join((st.get("people") or [])[:3]), "count": str(st.get("count", 0)),
            "method": group.get("method", "")}


def render(template, group, st):
    """! @brief Fill a template; collapses the spaces and separators empty fields leave."""
    f = fields(group, st)
    out = re.sub(r"\{(\w+)\}", lambda m: f.get(m.group(1), m.group(0)), template or "")
    out = re.sub(r"\s{2,}", " ", out)
    out = re.sub(r"(^[\s\-/,|]+|[\s\-/,|]+$)", "", out)
    out = re.sub(r"/{2,}", "/", out)
    return out.strip()


def fallback_name(group, st):
    """! @brief A name without the LLM: the place and the dates, or the people."""
    if group.get("method") == "people" and st.get("people"):
        return render("{people} {year}", group, st) or "Untitled"
    return render("{place} {range}", group, st) or render("{method} {count}", group, st) or "Untitled"


def summary_text(group, st):
    """! @brief The facts the LLM gets about a group."""
    parts = ["%d photos%s" % (st["count"], (" and videos (%d videos)" % st["videos"]) if st.get("videos") else "")]
    if st.get("start"):
        parts.append("taken %s (%d day%s)" % (date_range(st["start"], st["end"]), st["days"], "" if st["days"] == 1 else "s"))
    if st.get("cities") or st.get("regions") or st.get("countries"):
        parts.append("places: " + ", ".join((st.get("cities") or []) + (st.get("regions") or [])[:2]
                                            + (st.get("countries") or [])[:2]))
    parts.append("%d%% have GPS" % round(100 * st.get("gps", 0)))
    if st.get("people"):
        parts.append("people: " + ", ".join(st["people"]))
    if st.get("tags"):
        parts.append("common tags: " + ", ".join(st["tags"]))
    if group.get("name"):
        parts.append("current name: " + group["name"])
    return "; ".join(parts)


# -- the rule compiler -------------------------------------------------------------------
RULE_METHODS = {
    "trips": ("group_trips", {"gap_hours": 36, "min_files": 8, "max_km": 0, "home": "", "home_km": 0}),
    "events": ("group_events", {"gap_minutes": 120, "min_files": 5}),
    "places": ("group_places", {"level": "city", "per_year": True, "min_files": 5}),
    "people": ("group_people", {"mode": "same_set", "min_files": 5, "min_people": 2}),
    "similar": ("group_similar", {"threshold": 0.85, "min_files": 5, "window_hours": 0}),
}

RULE_PROMPT = """You configure an automatic photo-album builder. Pick ONE grouping method for the user's rule and
its settings, and write a yes/no question each candidate group should pass.

Methods and their settings (defaults in brackets):
- trips: photos close in time, away from home. gap_hours [36] max hours without a photo inside one trip,
  min_files [8], max_km [0 = off] split at a jump this far, home ["lat, lon" or ""], home_km [0].
- events: bursts of photos. gap_minutes [120], min_files [5].
- places: one group per place. level [city|admin1|country], per_year [true], min_files [5].
- people: by who is in the photos. mode [each|same_set], min_files [5], min_people [2].
- similar: photos that look alike. threshold [0.85, cosine 0..1], min_files [5], window_hours [0 = any time].

Answer with JSON only: {"method": "...", "settings": {...}, "check": "question, or empty for none",
"name_template": "album name template using {place} {range} {year} {month} {people} or empty"}

Rule: """


def compile_rule(rule, llm):
    """! @brief Natural-language rule -> {method, settings, check, name_template} (validated).
    Without an LLM, or on an unusable answer, picks trips with its defaults."""
    spec = None
    if llm and (rule or "").strip():
        try:
            spec = llm(RULE_PROMPT + rule.strip(), None, "json")
        except Exception:
            spec = None
    return clean_rule(spec)


def clean_rule(spec):
    """! @brief Keep only known methods and settings, typed like the defaults."""
    spec = spec if isinstance(spec, dict) else {}
    method = str(spec.get("method") or "trips").strip().lower()
    if method not in RULE_METHODS:
        method = "trips"
    defaults = RULE_METHODS[method][1]
    got = spec.get("settings") if isinstance(spec.get("settings"), dict) else {}
    settings = {}
    for k, d in defaults.items():
        v = got.get(k, d)
        try:
            if isinstance(d, bool):
                v = _truthy(v)
            elif isinstance(d, (int, float)):
                v = float(v)
            else:
                v = str(v or "")
        except (TypeError, ValueError):
            v = d
        settings[k] = v
    return {"method": method, "settings": settings, "check": str(spec.get("check") or "").strip(),
            "name_template": str(spec.get("name_template") or "").strip()}


# -- runner ---------------------------------------------------------------------------------
class Context:
    """! @brief What a run needs from the app. The module builds one; the tests a fake.
    lib       the Library facade (items, scope, vectors, album_members)
    llm       fn(prompt, image_bgr or None, want) or None when no LLM is configured
    collage   fn(rel_paths) -> BGR contact sheet or None
    report    fn(message) for the run log / progress
    """

    def __init__(self, lib, llm=None, collage=None, report=None, auto_albums=None):
        self.lib = lib
        self.llm = llm
        self.collage = collage or (lambda rels: None)
        self.report = report or (lambda msg: None)
        self.auto_albums = auto_albums or {}
        self._members = None

    def members(self):
        if self._members is None:
            self._members = self.lib.album_members()
        return self._members


def parse_home(text):
    """! @brief 'lat, lon' -> (lat, lon) or None."""
    m = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)\s*$", str(text or ""))
    return (float(m.group(1)), float(m.group(2))) if m else None


def run_graph(graph, ctx):
    """! @brief Evaluate a library/1 graph. @return {"plan": [...], "log": [...]}"""
    nodes = {n["id"]: n for n in (graph or {}).get("nodes", []) if n.get("id")}
    cache = {}
    log = []

    def report(msg):
        log.append(msg)
        ctx.report(msg)

    def wires(n, port):
        w = (n.get("in") or {}).get(port)
        if not w:
            return []
        return w if isinstance(w[0], (list, tuple)) else [w]

    def pull(n, p):
        vals = []
        for w in wires(n, p["name"]):
            src = nodes.get(w[0])
            if src is None:
                continue
            v = outputs(src).get(w[1])
            if v is not None:
                vals.append(v)
        return vals if p.get("multi") else (vals[0] if vals else None)

    def outputs(n):
        if n["id"] in cache:
            return cache[n["id"]]
        cache[n["id"]] = {}      # a cycle reads empty instead of recursing forever
        try:
            out = run_node(n) or {}
        except Cancelled:
            raise
        except Exception as e:
            report("%s failed: %s" % (n.get("label") or n["type"], e))
            out = {}
        cache[n["id"]] = out
        return out

    def run_node(n):
        t = n.get("type")
        spec = CATALOG.get(t)
        if spec is None:
            report("unknown node type %s" % t)
            return {}
        ins = {p["name"]: pull(n, p) for p in spec["inputs"]}
        if "run" in (n.get("in") or {}) and not _truthy(ins.get("run")):
            return {}
        for p in spec["inputs"]:
            if not p["optional"] and p["name"] != "run" and ins.get(p["name"]) is None:
                return {}
        fn = NODES.get(t)
        out = fn(n, ins, ctx, report) if fn else {}
        label = n.get("label") or spec["label"]
        for k, v in out.items():
            if isinstance(v, list) and k in ("files", "pass", "groups", "kept"):
                report("%s: %d %s" % (label, len(v), "groups" if k in ("groups", "kept") else "files"))
        return out

    plan = []
    for e in (n for n in nodes.values() if n.get("type") == "end"):
        for part in pull(e, CATALOG["end"]["inputs"][0]) or []:
            plan.extend(part)
    for i, entry in enumerate(plan):
        entry["id"] = i
    return {"plan": plan, "log": log}


# -- node implementations ---------------------------------------------------------------------
def _items(ctx, rels):
    return ctx.lib.items(rels)


def _by_rel(ctx, groups):
    rels = sorted({r for g in groups for r in g["files"]})
    return {it["rel"]: it for it in _items(ctx, rels)}


def n_start(n, ins, ctx, report):
    ctx.lib.use_file_dates = _flag(n, "use_file_dates")
    return {"files": ctx.lib.scope(n.get("q") or "", n.get("folder") or "", n.get("album") or "")}


def n_query(n, ins, ctx, report):
    return {"files": ctx.lib.scope(n.get("q") or "", n.get("folder") or "", n.get("album") or "")}


def n_filter(n, ins, ctx, report):
    keep, rest = [], []
    kind, has_date, has_gps, in_album = (n.get("kind") or "any", n.get("has_date") or "any",
                                         n.get("has_gps") or "any", n.get("in_album") or "any")
    min_rating = _num(n, "min_rating", None)
    person = (n.get("person") or "").strip().lower()
    tag = (n.get("tag") or "").strip().lower()
    folder = (n.get("folder") or "").strip().strip("/")
    for it in _items(ctx, ins["files"]):
        ok = (kind == "any" or it["kind"] == kind)
        ok = ok and (has_date == "any" or (it["t"] is not None) == (has_date == "yes"))
        ok = ok and (has_gps == "any" or (it["lat"] is not None) == (has_gps == "yes"))
        ok = ok and (in_album == "any" or bool(it["albums"]) == (in_album == "yes"))
        ok = ok and (min_rating is None or (it["rating"] or 0) >= min_rating)
        ok = ok and (not person or any(p.lower() == person for p in it["people"]))
        ok = ok and (not tag or any(x.lower() == tag for x in it["tags"]))
        ok = ok and (not folder or it["rel"].startswith(folder + "/"))
        (keep if ok else rest).append(it["rel"])
    return {"pass": keep, "rest": rest}


def n_union(n, ins, ctx, report):
    seen, out = set(), []
    for fs in ins["files"] or []:
        for r in fs:
            if r not in seen:
                seen.add(r)
                out.append(r)
    return {"files": out}


def n_group_trips(n, ins, ctx, report):
    return {"groups": cl.trips(_items(ctx, ins["files"]), gap_hours=_num(n, "gap_hours", 36.0),
                               min_files=int(_num(n, "min_files", 5)), home=parse_home(n.get("home")),
                               home_km=_num(n, "home_km", 0.0), max_km=_num(n, "max_km", 0.0))}


def n_group_events(n, ins, ctx, report):
    return {"groups": cl.events(_items(ctx, ins["files"]), gap_minutes=_num(n, "gap_minutes", 120.0),
                                min_files=int(_num(n, "min_files", 5)))}


def n_group_places(n, ins, ctx, report):
    return {"groups": cl.places(_items(ctx, ins["files"]), level=n.get("level") or "city",
                                per_year=_flag(n, "per_year"), min_files=int(_num(n, "min_files", 5)))}


def n_group_people(n, ins, ctx, report):
    return {"groups": cl.people(_items(ctx, ins["files"]), mode=n.get("mode") or "each",
                                min_files=int(_num(n, "min_files", 5)), min_people=int(_num(n, "min_people", 1)))}


def n_group_similar(n, ins, ctx, report):
    vecs = ctx.lib.vectors(ins["files"])
    if not vecs:
        report("look-alikes: no embeddings (run the embedding module first)")
        return {"groups": []}
    return {"groups": cl.similar(_items(ctx, ins["files"]), vecs, threshold=_num(n, "threshold", 0.85),
                                 min_files=int(_num(n, "min_files", 5)), window_hours=_num(n, "window_hours", 0.0))}


_RULE_RUNNERS = {"trips": n_group_trips, "events": n_group_events, "places": n_group_places,
                 "people": n_group_people, "similar": n_group_similar}


def n_group_rule(n, ins, ctx, report):
    spec = None
    if (n.get("compiled") or "").strip():
        try:
            spec = clean_rule(json.loads(n["compiled"]))
        except ValueError:
            spec = None
    if spec is None:
        spec = compile_rule(n.get("rule") or "", ctx.llm)
        n["compiled"] = json.dumps(spec)        # the module saves it back with the graph
        report("AI rule compiled: %s %s" % (spec["method"], json.dumps(spec["settings"])))
    groups = _RULE_RUNNERS[spec["method"]](dict(spec["settings"]), ins, ctx, report)["groups"]
    if spec["check"]:
        groups = n_ai_judge({"rule": spec["check"], "samples": 6}, {"groups": groups}, ctx, report)["kept"]
    if spec["name_template"]:
        groups = n_name_template({"template": spec["name_template"]}, {"groups": groups}, ctx, report)["groups"]
    return {"groups": groups, "rule": json.dumps(spec)}


def n_merge_groups(n, ins, ctx, report):
    groups = ins["groups"]
    by = _by_rel(ctx, groups)
    vecs = ctx.lib.vectors(list(by))
    if not vecs:
        return {"groups": groups}
    return {"groups": cl.merge_similar(groups, by, vecs, threshold=_num(n, "threshold", 0.8),
                                       max_gap_hours=_num(n, "max_gap_hours", 72.0))}


def n_filter_groups(n, ins, ctx, report):
    by = _by_rel(ctx, ins["groups"])
    kept, dropped = [], []
    skip = _num(n, "skip_in_album", 0.0)
    auto = set(ctx.auto_albums.values()) if ctx.auto_albums else set()
    for g in ins["groups"]:
        st = cl.stats(g, by)
        ok = st["count"] >= _num(n, "min_files", 0) and (not _num(n, "max_files", 0) or st["count"] <= _num(n, "max_files", 0))
        ok = ok and st["days"] >= _num(n, "min_days", 0) and (not _num(n, "max_days", 0) or st["days"] <= _num(n, "max_days", 0))
        ok = ok and st["gps"] >= _num(n, "min_gps", 0.0) and len(st["people"]) >= _num(n, "min_people", 0)
        if ok and skip > 0:
            # albums this pipeline made don't count: re-running updates them
            members = {a: m for a, m in ctx.members().items() if a not in auto}
            album, share = cl.album_overlap(g["files"], members)
            if share >= skip:
                ok = False
                report("skipped (in album %s): %d files" % (album, st["count"]))
        (kept if ok else dropped).append(g)
    return {"kept": kept, "dropped": dropped}


def n_union_groups(n, ins, ctx, report):
    seen, out = set(), []
    for gs in ins["groups"] or []:
        for g in gs:
            if g["id"] not in seen:
                seen.add(g["id"])
                out.append(g)
    return {"groups": out}


def _ask(ctx, prompt, group, samples):
    img = ctx.collage(cl.sample(group["files"], max(0, min(MAX_SAMPLES, int(samples))))) if samples else None
    return ctx.llm(prompt, img, "json")


def n_ai_judge(n, ins, ctx, report):
    groups = ins["groups"]
    if not ctx.llm:
        report("AI judge: no LLM configured, every group kept")
        return {"kept": groups, "rejected": []}
    by = _by_rel(ctx, groups)
    kept, rejected = [], []
    for i, g in enumerate(groups):
        st = cl.stats(g, by)
        prompt = ("You organise a photo library into albums. Rule: %s\n\nCandidate group: %s\n\nThe image is a "
                  "contact sheet of photos from the group. Answer with JSON only: {\"keep\": true or false, "
                  "\"name\": \"short album title, at most 6 words\", \"description\": \"one sentence\", "
                  "\"reason\": \"why\"}" % (n.get("rule") or "", summary_text(g, st)))
        ctx.report("AI judge %d/%d" % (i + 1, len(groups)))
        try:
            ans = _ask(ctx, prompt, g, _num(n, "samples", 6)) or {}
        except Cancelled:
            raise
        except Exception as e:
            report("AI judge failed (%s): group kept" % e)
            ans = {"keep": True}
        g = dict(g)
        if ans.get("name") and not g.get("name"):
            g["name"] = str(ans["name"]).strip()[:80]
        if ans.get("description") and not g.get("description"):
            g["description"] = str(ans["description"]).strip()[:500]
        g["reason"] = str(ans.get("reason") or "")[:300]
        (kept if _truthy(ans.get("keep", True)) else rejected).append(g)
    report("AI judge: kept %d, rejected %d" % (len(kept), len(rejected)))
    return {"kept": kept, "rejected": rejected}


def n_ai_name(n, ins, ctx, report):
    groups = ins["groups"]
    by = _by_rel(ctx, groups)
    overwrite = _flag(n, "overwrite")
    out = []
    for i, g in enumerate(groups):
        g = dict(g)
        st = cl.stats(g, by)
        if g.get("name") and not overwrite:
            out.append(g)
            continue
        ans = {}
        if ctx.llm:
            prompt = ((n.get("prompt") or "Name this photo album.") + "\n\nGroup: %s\n\nThe image is a contact "
                      "sheet of its photos. Answer with JSON only: {\"name\": \"short album title, at most 6 words, "
                      "no dates unless they matter\", \"description\": \"one sentence about what happened\"}"
                      % summary_text(g, st))
            ctx.report("AI name %d/%d" % (i + 1, len(groups)))
            try:
                ans = _ask(ctx, prompt, g, _num(n, "samples", 6)) or {}
            except Cancelled:
                raise
            except Exception as e:
                report("AI name failed: %s" % e)
        g["name"] = (str(ans.get("name") or "").strip()[:80]
                     or render(n.get("template") or "", g, st) or fallback_name(g, st))
        if ans.get("description") and (overwrite or not g.get("description")):
            g["description"] = str(ans["description"]).strip()[:500]
        out.append(g)
    return {"groups": out}


def n_name_template(n, ins, ctx, report):
    by = _by_rel(ctx, ins["groups"])
    out = []
    for g in ins["groups"]:
        g = dict(g)
        if not g.get("name") or _flag(n, "overwrite", True):
            st = cl.stats(g, by)
            g["name"] = render(n.get("template") or "{place} {range}", g, st) or fallback_name(g, st)
        out.append(g)
    return {"groups": out}


def _entry(op, g, st, by, **kw):
    """! @brief A plan entry with the group's facts and a few sample files for the preview."""
    e = {"op": op, "group": g["id"], "method": g.get("method", ""), "files": list(g["files"]),
         "name": g.get("name") or fallback_name(g, st), "description": g.get("description", ""),
         "reason": g.get("reason", ""),
         "facts": {k: st.get(k) for k in ("count", "start", "end", "days", "cities", "regions",
                                          "countries", "people", "gps", "center")},
         "samples": cl.sample(g["files"], 6)}
    e.update(kw)
    return e


def _cover(g, by, how):
    files = g["files"]
    if not files:
        return ""
    if how == "first":
        return files[0]
    if how == "middle":
        return files[len(files) // 2]
    rated = [by[r] for r in files if r in by and by[r].get("rating")]
    return max(rated, key=lambda it: it["rating"])["rel"] if rated else files[len(files) // 2]


def n_make_albums(n, ins, ctx, report):
    by = _by_rel(ctx, ins["groups"])
    plan = []
    for g in ins["groups"]:
        st = cl.stats(g, by)
        name = render(n.get("template") or "{name}", g, st) or g.get("name") or fallback_name(g, st)
        plan.append(_entry("album", g, st, by, album=name[:120], cover=_cover(g, by, n.get("cover") or "best"),
                           update_existing=_flag(n, "update_existing", True),
                           set_description=_flag(n, "description", True)))
    return {"plan": plan}


def n_tag_groups(n, ins, ctx, report):
    by = _by_rel(ctx, ins["groups"])
    plan = []
    for g in ins["groups"]:
        st = cl.stats(g, by)
        tag = render(n.get("template") or "{name}", g, st)
        if tag:
            plan.append(_entry("tag", g, st, by, tag=tag[:120]))
    return {"plan": plan}


_UNSAFE = re.compile(r'[<>:"\\|?*\x00-\x1f]')


def safe_folder(path):
    """! @brief A relative folder made of clean parts (no '..', no reserved characters)."""
    parts = []
    for p in str(path or "").replace("\\", "/").split("/"):
        p = _UNSAFE.sub("_", p).strip().strip(".")
        if p:
            parts.append(p[:100])
    return "/".join(parts)


def n_move_groups(n, ins, ctx, report):
    by = _by_rel(ctx, ins["groups"])
    plan = []
    for g in ins["groups"]:
        st = cl.stats(g, by)
        folder = safe_folder(render(n.get("template") or "{year}/{name}", g, st))
        if folder:
            plan.append(_entry("move", g, st, by, folder=folder))
    return {"plan": plan}


def n_photo_pipeline(n, ins, ctx, report):
    groups = list(ins.get("groups") or [])
    if ins.get("files"):
        groups.append(cl.make_group("files", ins["files"], name=""))
    by = _by_rel(ctx, groups)
    plan = []
    for g in groups:
        st = cl.stats(g, by)
        context = []
        if _flag(n, "context", True) and g["method"] != "files":
            if g.get("name"):
                context.append("Part of '%s'." % g["name"])
            if g.get("description"):
                context.append(g["description"])
            context.append(summary_text(dict(g, name=""), st))
        plan.append(_entry("photo", g, st, by, context=" ".join(context),
                           only_undescribed=_flag(n, "only_undescribed", True)))
    return {"plan": plan}


NODES = {"start": n_start, "query": n_query, "filter": n_filter, "union": n_union,
         "group_trips": n_group_trips, "group_events": n_group_events, "group_places": n_group_places,
         "group_people": n_group_people, "group_similar": n_group_similar, "group_rule": n_group_rule,
         "merge_groups": n_merge_groups, "filter_groups": n_filter_groups, "union_groups": n_union_groups,
         "ai_judge": n_ai_judge, "ai_name": n_ai_name, "name_template": n_name_template,
         "make_albums": n_make_albums, "tag_groups": n_tag_groups, "move_groups": n_move_groups,
         "photo_pipeline": n_photo_pipeline}