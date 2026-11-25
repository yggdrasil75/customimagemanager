"""! @file
@brief The person pipeline - the People scan as one ordered task pipeline:

    load -> detect faces -> detect people -> background regions -> landmarks
         -> face identity -> gender / age -> body identity -> save
         -> match known people -> names from metadata -> names via LLM -> finish

======================================================================
Each step is a task_pipeline step (a node of the Person pipeline graph in
Settings > Pipeline: rewire, switch off, pick a backend), run over a batch of
files at a time. It replaces the two
background sweeps (Face detection, Person detection) that each decoded the
image, ran their own model and left the other half to the other sweep: the
image is decoded once, a cheetah backend brings each person's body vector and
the face bound to it, and a detector that gives landmarks (SCRFD / YuNet) hands them
straight to recognition, which then aligns without detecting the face again.

An item is a dict:
    rel, abs, img (BGR)                       set by load
    faces   [{cx,cy,w,h,conf,kps?,_drawn}]    detect faces / landmarks
    persons [{cx,cy,w,h,conf,_reid?,_face_box?}]  detect people
    extra   [region dicts]                    background regions
    face_info [{vec, shape, gender, p_male, age}]  face identity / gender-age (per face)
    body_vecs [(vec, mode)]                   body identity (per person)
    face_ids [face_regions.id | None]         save
    hints   {face index: (name, source)}      naming steps
Names found by the naming steps are suggestions (face_regions.name_hint): the
People tab offers them, the user confirms. Nothing here writes a name.
"""

import contextlib
import json
import os
import re
import time

import numpy as np

import model_registry
from modules.model_broker import NoProviderError
from optional_deps import optional_import
from . import people_core as pc
from . import personlib

cv2, _HAVE_CV2 = optional_import("cv2")

PID = "person"
CAP = "people.pipeline"
_STEP = "people."

# -- helpers ----------------------------------------------------------------------

def _svc():
    """! @brief The task_pipeline service ({} when absent)."""
    return pc.HOST.get_service("task_pipeline") or {}


def _opt(step, key, default=None):
    """! @brief One option of a person-pipeline step, as the user set it."""
    s = _svc()
    return s["step_option"](PID, _STEP + step, key, default) if s else default


def _on(step):
    """! @brief Whether a person-pipeline step is switched on."""
    s = _svc()
    return bool(s and s["step_enabled"](PID, _STEP + step))


def _provider_choices(cap):
    """! @brief Select choices for a step's backend: the Models tab pick, then every
    registered provider (greyed with its reason when unavailable)."""
    try:
        provs = pc.HOST.broker.providers_for(cap, quick=True)
        picked = pc.HOST.broker.selected_id(cap)
    except Exception:
        provs, picked = [], None
    lab = next((p["label"] for p in provs if p["id"] == picked), picked or "none")
    return ([{"value": "picked", "label": f"Models tab pick ({lab})"}] +
            [{"value": p["id"], "label": p["label"], "available": bool(p["available"]),
              "reason": p.get("reason") or ""} for p in provs])


def _backend(cap, opt_value):
    """! @brief The provider id a step will use: its own pick, or the Models tab's."""
    if opt_value and opt_value != "picked":
        return opt_value
    return pc.HOST.broker.selected_id(cap)


def _cheetah(it, env):
    """! @brief One cheetah pass per image: people with body vectors, bound to the faces
    the face step already found (no second face detection)."""
    if "_cheetah" not in it:
        fs = pc._faces() or {}
        if not fs.get("cheetah_analyze"):
            it["_cheetah"] = {"faces": [], "persons": []}
        else:
            if not env.get("_cheetah_ready"):
                fs["cheetah_ready"]()
                env["_cheetah_ready"] = True
            faces = it["faces"] if env.get("faces", True) else None
            it["_cheetah"] = fs["cheetah_analyze"](it["img"], 0.4, faces)
    return it["_cheetah"]


def _timed(key):
    """! @brief Context manager adding the block's time to one of the scan log phases."""
    @contextlib.contextmanager
    def cm():
        """! @brief The timing context itself."""
        t = time.time()
        try:
            yield
        finally:
            pc._face_t[key] = pc._face_t.get(key, 0.0) + time.time() - t
    return cm()


def _region(b, cls):
    """! @brief The MWG region dict the file gets for a detection."""
    r = {"class_name": cls, "region_name": "", "cx": b["cx"], "cy": b["cy"], "w": b["w"],
         "h": b["h"], "confirmed": False, "region_tags": [], "region_description": "",
         "conf": b.get("conf")}
    for k in ("mask_svg", "polygon"):
        if b.get(k) is not None:
            r[k] = b[k]
    return r


# -- steps ------------------------------------------------------------------------

def step_load(items, o, env):
    """! @brief Resolve, apply the skip rules, decode once to 3-channel BGR."""
    for it in items:
        it.update(faces=[], persons=[], extra=[], face_info=[], body_vecs=[], face_ids=[], hints={})
        rel = it["rel"]
        abs_p = pc.get_safe_path(pc.MEDIA_DIR, rel)
        if not abs_p or not os.path.exists(abs_p):
            it["_stop"] = "missing"
            continue
        why = pc._skip_scan_reason(rel)
        if why:
            pc._face_log("skip %s (%s)", rel, why)
            it["_stop"] = why
            continue
        with _timed("decode"):
            try:
                img = pc.read_jxl(abs_p)
                img = pc._to_bgr(img) if img is not None else None
            except Exception as e:
                pc._face_err("decode failed (%s): %s", rel, e)
                img = None
        if img is None:
            it["_stop"] = "decode"
            continue
        it["abs"], it["img"] = abs_p, img


def step_detect_faces(items, o, env):
    """! @brief Face boxes (+ 5 landmarks from SCRFD / YuNet), batched when the
    backend can, drawn-face filtered by the faces module."""
    if env.get("faces") is False:
        return
    conf = float(o.get("conf") or 0)
    kw = {"conf": conf} if conf > 0 else {}
    with _timed("detect"):
        run = env.get("face_run")
        pid = None if run else _backend("detect.faces", o.get("backend"))
        try:
            run = run or pc.HOST.broker.request("detect.faces", provider=pid)
        except NoProviderError as e:
            pc._face_err("detect.faces: %s", e)
            return
        imgs = [it["img"] for it in items]
        try:
            batches = (run.batch(imgs, **kw) if hasattr(run, "batch")
                       else [run(im, **kw) for im in imgs])
        except Exception as e:
            pc._face_err("detect.faces batch: %s", e)
            batches = [[] for _ in imgs]
    for it, boxes in zip(items, batches):
        it["faces"] = [dict(b) for b in boxes or []]


def step_detect_persons(items, o, env):
    """! @brief Person boxes; the cheetah backend brings each one's body vector along."""
    if env.get("bodies") is False:
        return
    with _timed("detect"):
        run = env.get("person_run")
        pid = None if run else _backend("detect.persons", o.get("backend"))
        conf = float(o.get("conf") or 0) or float(pc.HOST.broker.variant("detect.persons")["conf"] or 0.25)
        if pid == "cheetah":
            for it in items:
                it["persons"] = [dict(p) for p in _cheetah(it, env)["persons"] if p.get("conf", 1) >= conf]
            return
        try:
            run = run or pc.HOST.broker.request("detect.persons", provider=pid)
        except NoProviderError:
            return
        imgs = [it["img"] for it in items]
        try:
            batches = (run.batch(imgs, conf=conf) if hasattr(run, "batch")
                       else [run(im, conf=conf) for im in imgs])
        except Exception as e:
            pc._face_err("detect.persons batch: %s", e)
            batches = [[] for _ in imgs]
    for it, boxes in zip(items, batches):
        it["persons"] = [dict(b, class_name="person") for b in boxes or []]


def step_background(items, o, env):
    """! @brief Region-shaped background capabilities (Models tab detect / segment with
    'run in background') ride the scan: person masks join person boxes, other
    classes become their own regions."""
    if env.get("faces") is False:
        return
    for it in items:
        try:
            insts = pc._background_instances(it["img"])
        except Exception as e:
            pc._face_err("background regions (%s): %s", it["rel"], e)
            continue
        if insts:
            pc._fold_background(insts, it["persons"], it["extra"])


def step_landmarks(items, o, env):
    """! @brief 5 landmarks for face boxes that came without them (YOLO-face): SCRFD on a
    crop around each box. Recognition aligns on them instead of re-detecting."""
    fs = pc._faces() or {}
    if not fs.get("refine_kps"):
        return
    for it in items:
        if it["faces"] and not all(f.get("kps") for f in it["faces"]):
            with _timed("embed"):
                fs["refine_kps"](it["img"], it["faces"])


def step_embed_faces(items, o, env):
    """! @brief Identity vector + face geometry per face (the embed.faces pick); faces the
    user tombstoned as 'not a face' are dropped first."""
    fs = pc._faces() or {}
    for it in items:
        it["faces"] = pc._drop_tombstoned(it["rel"], it["faces"])
        if not it["faces"] or not fs:
            continue
        with _timed("embed"):
            vecs, mode, shapes = fs["embed_faces"](it["img"], it["faces"], want_shape=True)
        it["face_mode"] = mode
        it["face_info"] = [{"vec": v, "shape": s, "gender": None, "p_male": None, "age": None}
                           for v, s in zip(vecs, shapes)]


def step_attributes(items, o, env):
    """! @brief Gender and age estimate per face from the pack's genderage head."""
    fs = pc._faces() or {}
    if not fs.get("face_attributes"):
        return
    for it in items:
        if not it["faces"]:
            continue
        with _timed("embed"):
            attrs = fs["face_attributes"](it["img"], it["faces"])
        while len(it["face_info"]) < len(it["faces"]):
            it["face_info"].append({"vec": None, "shape": None, "gender": None, "p_male": None, "age": None})
        for info, a in zip(it["face_info"], attrs):
            info.update(a)


def step_embed_bodies(items, o, env):
    """! @brief Body identity per person: the vector cheetah already gave, else the
    embed.bodies pick."""
    bs = pc._bodies()
    for it in items:
        ps = it["persons"]
        out = [None] * len(ps)
        rest = []
        for i, p in enumerate(ps):
            if p.get("_reid") is not None:
                out[i] = (np.asarray(p["_reid"], np.float32), "cheetah-reid")
            else:
                rest.append(i)
        if rest and bs:
            with _timed("embed"):
                vecs, mode = bs["embed_bodies"](it["img"], [ps[i] for i in rest])
            for i, v in zip(rest, vecs or []):
                out[i] = (v, mode) if v is not None else None
        it["body_vecs"] = out


def _iou(a, b):
    """! @brief IoU of two normalised centre-form boxes."""
    ax1, ay1, ax2, ay2 = a["cx"] - a["w"] / 2, a["cy"] - a["h"] / 2, a["cx"] + a["w"] / 2, a["cy"] + a["h"] / 2
    bx1, by1, bx2, by2 = b["cx"] - b["w"] / 2, b["cy"] - b["h"] / 2, b["cx"] + b["w"] / 2, b["cy"] + b["h"] / 2
    iw, ih = max(0.0, min(ax2, bx2) - max(ax1, bx1)), max(0.0, min(ay2, by2) - max(ay1, by1))
    u = a["w"] * a["h"] + b["w"] * b["h"] - iw * ih
    return iw * ih / u if u > 0 else 0.0


def _bind_detector_faces(it):
    """! @brief A person cheetah bound to a face points at that face in this item's face
    list (`_face` index), whichever detector produced the list: the kept face that
    overlaps cheetah's face box. No overlap (filtered, or another detector missed
    it) leaves the binding to geometry."""
    for p in it["persons"]:
        fb = p.pop("_face_box", None)
        if fb is None:
            continue
        best, bi = 0.5, None
        for j, f in enumerate(it["faces"]):
            o = _iou(fb, f)
            if o >= best:
                best, bi = o, j
        p["_face"] = bi


def step_save(items, o, env):
    """! @brief Regions into the file (merged with what it has, so named boxes survive a
    rescan), face / body rows into the cache with landmarks, gender and age."""
    for it in items:
        rel, img = it["rel"], it["img"]
        found = ([_region(f, "face") | {"_drawn": f.get("_drawn")} for f in it["faces"]] +
                 it["extra"] + [_region(p, "person") for p in it["persons"]])
        if not found:
            continue
        pc._scan_saving.add(rel)
        try:
            with _timed("meta"):
                meta = pc.read_metadata(it["abs"])
                merged = pc._merge_regions(meta["regions"], found)
                pc.update_file(it["abs"], set={"regions": merged}, meta=meta)
            with _timed("embed"):
                if it["faces"]:
                    it["face_ids"] = pc._store_faces(rel, img, it["faces"], it["face_info"],
                                                     it.get("face_mode") or "none")
                    pc._face_dirty["v"] = True
        finally:
            pc._scan_saving.discard(rel)
            if it["persons"] and any(it["body_vecs"]):
                _bind_detector_faces(it)
                pc._store_bodies(rel, it["persons"], it["body_vecs"], it["faces"], it["face_ids"])
                pc._body_dirty["v"] = True


def _named_centroids(env):
    """! @brief {cluster_id: (unit centroid, name)} over confirmed, named identity rows;
    built once per run."""
    if "_centroids" in env:
        return env["_centroids"]
    acc = {}
    for cid, name, blob in pc._db().execute(
            "SELECT cluster_id, name, embedding FROM face_regions WHERE confirmed=1 AND name<>'' "
            "AND cluster_id>=0 AND embedding IS NOT NULL AND embed_mode='arcface'").fetchall():
        v = np.frombuffer(blob, np.float32)
        s = acc.setdefault(cid, [np.zeros_like(v), name, 0])
        if s[0].shape == v.shape:
            s[0] += v
            s[2] += 1
    out = {}
    for cid, (tot, name, n) in acc.items():
        nrm = float(np.linalg.norm(tot))
        if n and nrm:
            out[cid] = (tot / nrm, name)
    env["_centroids"] = out
    return out


# a face row automation may still touch: no user decision on it (see _user_kept_rows)
_AUTO_OK = ("COALESCE(confirmed,0)=0 AND COALESCE(unknown,0)=0 AND COALESCE(not_face,0)=0 "
            "AND COALESCE(user_set,0)=0")


def step_match_known(items, o, env):
    """! @brief A new face close enough to a named person joins that person's cluster now
    (instead of after the next full recluster) and gets the name as a suggestion."""
    eps = float(o.get("max_distance") or 0.35)
    cents = None
    for it in items:
        if it.get("face_mode") != "arcface" or pc.people_excluded(it["rel"]):
            continue                      # a cover's faces don't join a person
        kept = pc._user_kept_rows(it["face_ids"])
        for i, (fid, info) in enumerate(zip(it["face_ids"], it["face_info"])):
            if fid is None or fid in kept or info.get("vec") is None:
                continue
            cents = cents if cents is not None else _named_centroids(env)
            if not cents:
                return
            v = np.asarray(info["vec"], np.float32)
            best, bd = None, 9.0
            for cid, (c, name) in cents.items():
                if c.shape != v.shape:
                    continue
                d = 1.0 - float(c @ v)
                if d < bd:
                    best, bd = (cid, name), d
            if best is not None and bd <= eps:
                pc._rows("face_regions", "id=? AND " + _AUTO_OK, (fid,),
                         set={"cluster_id": best[0]})
                it["hints"][i] = (best[1], "cluster")
    pc._db().commit()


_SPLIT = re.compile(r"\s*[,;|]\s*")


def _known_names(env):
    """! @brief Lower-case -> spelled name of every known person (confirmed names, person records); once per run."""
    if "_names" not in env:
        names = {r[0].strip() for r in pc._db().execute(
            "SELECT DISTINCT name FROM face_regions WHERE confirmed=1 AND name<>''").fetchall()}
        try:
            names |= {(d.get("name") or "").strip() for d in personlib.list_all(pc.MEDIA_DIR)}
        except Exception:
            pass
        env["_names"] = {n.lower(): n for n in names if n and n.lower() not in pc._NOT_A_NAME}
    return env["_names"]


def _image_names(it, o, env):
    """! @brief Names the file's own metadata puts in the picture: its persons field (any
    name), tags naming a known person (last segment of a hierarchical tag), and
    known names written in the description."""
    meta = it.get("_meta")
    if meta is None:
        try:
            meta = it["_meta"] = pc.read_metadata(it["abs"])
        except Exception:
            return []
    known = _known_names(env)
    out = []

    def add(n):
        """! @brief Append a name once (case-insensitive), spelled as the known person is."""
        n = (n or "").strip()
        if n and n.lower() not in pc._NOT_A_NAME and n.lower() not in [x.lower() for x in out]:
            out.append(known.get(n.lower(), n))

    for n in _SPLIT.split(str(meta.get("persons") or "")):
        add(n)
    if o.get("tags", True):
        for t in meta.get("tags") or []:
            last = re.split(r"[/|:>]", str(t))[-1].strip()
            if last.lower() in known:
                add(last)
    if o.get("description", True) and known:
        desc = str(meta.get("description") or "").lower()
        for low, n in known.items():
            if len(low) >= 3 and re.search(r"(?<!\w)" + re.escape(low) + r"(?!\w)", desc):
                add(n)
    return out


def _unnamed(it):
    """! @brief (face index, row id) of this file's faces with no confirmed name and no
    suggestion yet, plus the names those faces already carry."""
    rows = {r[0]: (r[1] or "", r[2]) for r in pc._db().execute(
        "SELECT id, name, confirmed FROM face_regions WHERE rel_path=?", (it["rel"],)).fetchall()}
    taken = {n.lower() for n, c in rows.values() if c and n}
    taken |= {h[0].lower() for h in it["hints"].values()}
    kept = pc._user_kept_rows(it["face_ids"])
    free = [(i, fid) for i, fid in enumerate(it["face_ids"])
            if fid is not None and fid not in kept and i not in it["hints"]]
    return free, taken


def step_name_metadata(items, o, env):
    """! @brief One unnamed face and one person the metadata names: that's who it is
    (a suggestion). More than one of either is left to the LLM step."""
    for it in items:
        if not it["face_ids"]:
            continue
        free, taken = _unnamed(it)
        if not free:
            continue
        names = [n for n in _image_names(it, o, env) if n.lower() not in taken]
        if not names:
            continue
        if len(free) == 1 and len(names) == 1:
            it["hints"][free[0][0]] = (names[0], "metadata")
        else:
            it["_ambiguous"] = names


def _numbered(img, faces, idxs):
    """! @brief A copy of the image with the given faces boxed and numbered 1..n."""
    out = img.copy()
    H, W = out.shape[:2]
    th = max(2, int(round(max(H, W) / 400)))
    for n, i in enumerate(idxs, 1):
        f = faces[i]
        x1, y1 = int((f["cx"] - f["w"] / 2) * W), int((f["cy"] - f["h"] / 2) * H)
        x2, y2 = int((f["cx"] + f["w"] / 2) * W), int((f["cy"] + f["h"] / 2) * H)
        cv2.rectangle(out, (x1, y1), (x2, y2), (0, 255, 255), th)
        cv2.putText(out, str(n), (x1, max(12 * th, y1 - 3 * th)), cv2.FONT_HERSHEY_SIMPLEX,
                    0.5 * th, (0, 255, 255), th)
    return out


def step_name_llm(items, o, env):
    """! @brief Several unnamed faces and several names in the metadata: the vision LLM is
    shown the faces numbered and asked which name goes with which (suggestions)."""
    llm = pc.HOST.get_service("llm")
    if not llm or not _HAVE_CV2:
        return
    cap = int(o.get("max_faces") or 6)
    for it in items:
        names = it.get("_ambiguous")
        if not names:
            continue
        free, _ = _unnamed(it)
        free = free[:cap]
        if not free:
            continue
        idxs = [i for i, _ in free]
        prompt = (f"The picture has {len(idxs)} face(s) outlined in yellow and numbered 1 to {len(idxs)}. "
                  f"These people may appear in it: {', '.join(names)}. "
                  "Answer with a JSON object mapping each face number to the one name it is, "
                  "or null when no name fits. Use each name at most once. Example: "
                  '{"1": "' + names[0] + '", "2": null}')
        try:
            ans = llm["call"](prompt, _numbered(it["img"], it["faces"], idxs), "json")
        except Exception as e:
            pc._face_err("name via llm (%s): %s", it["rel"], e)
            continue
        if isinstance(ans, str):
            try:
                ans = json.loads(ans[ans.find("{"):ans.rfind("}") + 1])
            except ValueError:
                ans = None
        if not isinstance(ans, dict):
            continue
        allowed, used = {n.lower(): n for n in names}, set()
        for k, v in ans.items():
            try:
                n = int(str(k).strip().lstrip("#"))
            except ValueError:
                continue
            name = allowed.get(str(v or "").strip().lower())
            if 1 <= n <= len(idxs) and name and name.lower() not in used:
                used.add(name.lower())
                it["hints"][idxs[n - 1]] = (name, "llm")


def step_finish(items, o, env):
    """! @brief Write the suggestions, mark every file of the batch done (stopped ones too,
    so a bad file can't wedge the queue)."""
    for it in items:
        for i, (name, src) in (it.get("hints") or {}).items():
            fid = it["face_ids"][i] if i < len(it.get("face_ids") or []) else None
            if fid is not None:
                pc._rows("face_regions", "id=? AND " + _AUTO_OK, (fid,),
                         set={"name_hint": name, "name_hint_src": src})
        done = {k: 1 for k, on in (("face_done", env.get("faces", True)),
                                   ("body_done", env.get("bodies", True))) if on}
        try:
            if done:
                pc.update_file(it["rel"], db=done, dont_write=True)
        except Exception as e:
            pc._face_err("mark-done FAILED for %s: %s", it["rel"], e)
        it.pop("img", None)
        it.pop("_cheetah", None)
    pc._db().commit()


# -- body pose ----------------------------------------------------------------------

def step_pose(items, o, env):
    """! @brief Body pose per image from the image already in memory (the pose module's
    'pose' capability: YOLO pose, RTMPose, MediaPipe, ViTPose...), stored in the sidecar
    and linked to the body regions just saved; marks the file done for the pose
    module's own sweep so it isn't estimated twice."""
    ps = pc.HOST.get_service("pose.run_image")
    if not ps:
        return
    for it in items:
        if it.get("img") is None:
            continue
        if o.get("only_with_people", True) and not it.get("persons"):
            continue
        try:
            with _timed("pose"):
                ps(it["rel"], it["abs"], it["img"])
        except Exception as e:
            pc._face_err("pose FAILED for %s: %s", it["rel"], e)


# -- can a step run here ----------------------------------------------------------

def _given(env, key):
    """! @brief ok when the run was handed this detector (run_batch's face_run / person_run):
    then no installed model is needed."""
    return {"ok": True, "reason": ""} if (env or {}).get(key) else None


def _cap_status(cap, what):
    """! @brief {"ok", "reason"}: a capability has at least one provider that can run."""
    try:
        provs = pc.HOST.broker.providers_for(cap, quick=True)
    except Exception:
        provs = []
    if not provs:
        return {"ok": False, "reason": f"no {what} model is installed (Models tab)"}
    if any(p.get("available") for p in provs):
        return {"ok": True, "reason": ""}
    first = provs[0]
    return {"ok": False, "reason": f"no {what} model can run: {first.get('label')}: "
                                    f"{first.get('reason') or 'unavailable'}"}


def _faces_status(key=None, what="the Faces module"):
    fs = pc._faces()
    if not fs:
        return {"ok": False, "reason": "the Faces module is off (Settings > Modules)"}
    if key and not fs.get(key):
        return {"ok": False, "reason": f"{what} is not available"}
    return {"ok": True, "reason": ""}


def _bodies_status():
    if pc._bodies() or _cap_status("detect.persons", "")["ok"]:
        return {"ok": True, "reason": ""}
    return {"ok": False, "reason": "the Bodies module is off and no people detector brings body vectors"}


def _llm_status():
    llm = pc.HOST.get_service("llm")
    if not llm:
        return {"ok": False, "reason": "no LLM module is on (Settings > Modules)"}
    if not _HAVE_CV2:
        return {"ok": False, "reason": "OpenCV (cv2) is not installed"}
    return {"ok": True, "reason": ""}


def _pose_status():
    if not pc.HOST.get_service("pose.run_image"):
        return {"ok": False, "reason": "the Pose module is off (Settings > Modules)"}
    return _cap_status("pose", "pose")


# -- registration -----------------------------------------------------------------

_CONF = {"key": "conf", "label": "Min confidence (0 = model default)", "kind": "number",
         "default": 0.0, "min": 0.0, "max": 1.0}

STEPS = [
    {"id": "people.load", "label": "Load image", "locked": True, "run": step_load,
     "summary": "Decode once to BGR; skip AI-generated / skip-tagged files (People settings)."},
    {"id": "people.detect_faces", "label": "Detect faces", "run": step_detect_faces,
     "summary": "Face boxes. SCRFD and YuNet also give 5 landmarks, so recognition needs no "
                "second detection. Drawn faces are filtered (Faces settings).",
     "status": lambda env=None: _given(env, "face_run") or _cap_status("detect.faces", "face detection"),
     "options": [{"key": "backend", "label": "Detector", "kind": "select", "default": "picked",
                  "choices": lambda: _provider_choices("detect.faces")}, _CONF]},
    {"id": "people.detect_persons", "label": "Detect people", "run": step_detect_persons,
     "summary": "Person boxes for body identity. Cheetah brings a body vector with each "
                "person and binds it to the face found in it (runs on the app's own runtime, "
                "AMD included: Faces settings > Cheetah runtime).",
     "status": lambda env=None: _given(env, "person_run") or _cap_status("detect.persons", "people detection"),
     "options": [{"key": "backend", "label": "Detector", "kind": "select", "default": "picked",
                  "choices": lambda: _provider_choices("detect.persons")}, _CONF]},
    {"id": "people.background", "label": "Background regions", "run": step_background,
     "summary": "Detect / segment models switched to 'run in background' in the Models tab "
                "add their regions here (person masks join person boxes)."},
    {"id": "people.landmarks", "label": "Landmarks for boxes without", "run": step_landmarks,
     "status": lambda: _faces_status("refine_kps", "SCRFD (for landmarks)"),
     "summary": "YOLO-face boxes carry no landmarks: SCRFD on a crop around each finds them."},
    {"id": "people.embed_faces", "label": "Face identity", "run": step_embed_faces,
     "status": lambda: _faces_status("embed_faces", "a face identity model"),
     "summary": "ArcFace vector per face (Models tab: Face identity), aligned on its landmarks. "
                "The People tab clusters these; without it no face is stored."},
    {"id": "people.attributes", "label": "Gender and age", "run": step_attributes,
     "status": lambda: _faces_status("face_attributes", "a gender / age model (insightface pack)"),
     "summary": "Per face: gender with the model's confidence, and an age estimate. The "
                "People tab shows a person's gender only when confident faces agree "
                "(People settings), and a birth-year range from photo date minus age "
                "over dated photos."},
    {"id": "people.embed_bodies", "label": "Body identity", "run": step_embed_bodies,
     "status": _bodies_status,
     "summary": "Body vector per person (cheetah's own, else Models tab: Body identity) that "
                "carries a name to photos where the face is turned or too small."},
    {"id": "people.save", "label": "Save regions and identities", "locked": True, "run": step_save,
     "summary": "Regions into the file (named boxes survive a rescan), vectors, landmarks, "
                "gender and age into the People cache."},
    {"id": "people.pose", "label": "Body pose", "run": step_pose, "status": _pose_status,
     "summary": "Skeleton keypoints per person (Models tab: Pose), from the image already "
                "loaded, stored in the file and linked to each body region. The pose "
                "module's own background run then skips these files.",
     "options": [{"key": "only_with_people", "label": "Only images with people found", "kind": "toggle",
                  "default": True,
                  "help": "Skip images the people detector found nobody in (saves a model call)."}]},
    {"id": "people.match_known", "label": "Match known people", "run": step_match_known,
     "summary": "A new face this close to a named person joins them right away and gets "
                "the name as a suggestion.",
     "options": [{"key": "max_distance", "label": "Max cosine distance", "kind": "number",
                  "default": 0.35, "min": 0.05, "max": 0.8}]},
    {"id": "people.name_metadata", "label": "Names from tags / description", "run": step_name_metadata,
     "summary": "One unnamed face and one person named by the file's persons field, a tag "
                "or the description: suggest that name.",
     "options": [{"key": "tags", "label": "Tags", "kind": "toggle", "default": True},
                 {"key": "description", "label": "Description", "kind": "toggle", "default": True}]},
    {"id": "people.name_llm", "label": "Names via LLM", "run": step_name_llm, "status": _llm_status,
     "off_note": "it costs one LLM call per file with several unnamed faces",
     "summary": "Several unnamed faces and several named people: ask the vision LLM which is "
                "which (the faces are shown numbered). Costs one LLM call per such file.",
     "options": [{"key": "max_faces", "label": "Max faces per file", "kind": "number",
                  "default": 6, "min": 1, "max": 20}]},
    {"id": "people.finish", "label": "Finish", "locked": True, "always": True, "run": step_finish,
     "summary": "Store suggestions, mark the files done."},
]

# the LLM step costs a call per ambiguous file, so it starts switched off
DEFAULT = [{"step": s["id"], "enabled": s["id"] != "people.name_llm"} for s in STEPS]


def register(host):
    """! @brief Steps + the 'person' pipeline on the task_pipeline service."""
    svc = host.get_service("task_pipeline")
    if not svc:
        host.logger.warning("people: task_pipeline service missing; person pipeline not registered")
        return
    for s in STEPS:
        svc["register_step"]({**s, "module_id": "people"})
    svc["register_pipeline"](
        PID, label="Person pipeline", steps=DEFAULT, module_id="people",
        summary="The People scan: what finds faces and people in every image, how they are "
                "identified, and how names are suggested. Runs as Models tab > People "
                "pipeline > Run in background, and for People > Scan faces.")


def face_backend():
    """! @brief The detect.faces provider the pipeline's face step uses."""
    return _backend("detect.faces", _opt("detect_faces", "backend"))


def bodies_on():
    """! @brief Whether the scan produces bodies (people detection and body identity on)."""
    return _on("detect_persons") and _on("embed_bodies")


def lease_keys():
    """! @brief Registry keys the run touches per image, to lease resident for a batch so
    the detector and recognizer don't evict each other on a small model budget."""
    keys = []
    fs = pc._faces() or {}
    if _on("detect_faces"):
        pid = _backend("detect.faces", _opt("detect_faces", "backend"))
        if pid != "scrfd":
            try:
                keys.append(getattr(pc.HOST.broker.request("detect.faces", provider=pid), "registry_key", None))
            except Exception:
                pass
    if fs:
        keys.append(fs["insight_registry_key"]())
    if _on("detect_persons"):
        pid = _backend("detect.persons", _opt("detect_persons", "backend"))
        if pid == "cheetah":
            keys.append(fs.get("cheetah_registry_key", lambda: None)())
        else:
            try:
                keys.append(getattr(pc.HOST.broker.request("detect.persons", provider=pid), "registry_key", None))
            except Exception:
                pass
            if _on("embed_bodies") and pc._bodies():
                keys.append(pc._bodies()["reid_registry_key"]())
    seen = set()
    return [k for k in keys if k and not (k in seen or seen.add(k))]


def run_batch(rels, face_run=None, person_run=None, faces=True, bodies=None):
    """! @brief Run the person pipeline over a batch of rel_paths.
    @param face_run / person_run  detector handles to use instead of the steps' picks
                                  (tests, callers holding a handle).
    @param faces / bodies         False skips that half of detection.
    @return number of files that failed (they are still marked done).
    """
    items = [{"rel": r} for r in rels]
    env = {"face_run": face_run, "person_run": person_run, "faces": faces is not False,
           "bodies": bodies_on() if bodies is None else bool(bodies)}
    svc = _svc()
    if not svc:
        for it in items:
            pc.update_file(it["rel"], db={"face_done": 1, "body_done": 1}, dont_write=True)
        return len(items)
    keys = lease_keys()
    ctx = contextlib.nullcontext()
    try:
        ctx = model_registry.lease(*keys) if keys else ctx
        ctx.__enter__()
    except Exception as e:
        # no lease (a model refused to load): run anyway, the steps degrade per model
        pc._face_err("person pipeline: lease failed (%s); running unleased", e)
        ctx = contextlib.nullcontext()
    try:
        rep = svc["run"](PID, items, env)
    finally:
        try:
            ctx.__exit__(None, None, None)
        except Exception:
            pass
    failed = sum(1 for s in rep["steps"] if s.get("error"))
    if failed:
        pc._face_err("person pipeline: %s", "; ".join(f"{s['step']}: {s['error']}"
                                                     for s in rep["steps"] if s.get("error")))
    return len(items) if failed else 0


# -- the background sweep (Models tab > People pipeline) ---------------------------

def _pending_where():
    """! @brief SQL predicate of a file the background sweep still has to run."""
    return ("(COALESCE(face_done,0)=0 OR COALESCE(body_done,0)=0)" if bodies_on()
            else "COALESCE(face_done,0)=0")


def sweep_pending(db, n, after=""):
    """! @brief Background sweep: the next files to run; reclusters once the queue drains."""
    rows = db.execute(f"SELECT rel_path FROM files WHERE {_pending_where()} "
                      "AND media_kind='image' AND rel_path > ? ORDER BY rel_path LIMIT ?",
                      (after, n)).fetchall()
    if not rows and not after:
        if pc._face_dirty["v"]:
            pc._face_dirty["v"] = False
            pc.HOST.set_status("Person pipeline: clustering faces...")
            pc.HOST.set_status(f"Person pipeline: done ({pc._recluster()} face cluster(s)).")
        if pc._body_dirty["v"]:
            pc._body_dirty["v"] = False
            pc._recluster_bodies()
    return [r[0] for r in rows]


def sweep_run(rels, abs_paths, handle):
    """! @brief Run the batch, each file only for the half it still lacks (people
    switched on later must not re-detect every face)."""
    qs = ",".join("?" * len(rels))
    need = {r[0]: (not r[1], not r[2]) for r in pc._db().execute(
        f"SELECT rel_path, COALESCE(face_done,0), COALESCE(body_done,0) FROM files "
        f"WHERE rel_path IN ({qs})", tuple(rels)).fetchall()}
    want_bodies = bodies_on()
    groups = {}
    for r in rels:
        f, b = need.get(r, (True, True))
        groups.setdefault((f, b and want_bodies), []).append(r)
    for (f, b), part in groups.items():
        if f or b:
            run_batch(part, faces=f, bodies=b)


def sweep_progress(db):
    """! @brief Background sweep: done / total over the library's images."""
    r = db.execute(f"SELECT COUNT(*), SUM(CASE WHEN {_pending_where()} THEN 0 ELSE 1 END) "
                   "FROM files WHERE media_kind='image'").fetchone()
    return {"done": int(r[1] or 0), "total": int(r[0] or 0)}


def register_sweep(host, batch):
    """! @brief The People pipeline capability (its background switch) and its sweep."""
    host.declare_capability(
        CAP, label="People pipeline", background=True, group="people",
        summary="The People scan as one pass per image: faces, people, identity, gender / age "
                "and name suggestions (steps and backends: the Person pipeline graph in Settings > Pipeline).",
        input="rel_paths (the background sweep)", output="face_regions / body_regions rows, regions")
    host.provide_model(
        CAP, "task", label="Person pipeline", family="Task pipeline", speed="balanced",
        supports_conf=False,
        note="Runs the Person pipeline graph (Settings > Pipeline). Its detectors and "
             "embedders are the providers picked there (or here in the Models tab).",
        loader=lambda: (lambda *a, **k: None), transform=None,
        available=lambda: True, reason="", cost_mb=0)
    host.add_background_sweep(CAP, sweep_pending, sweep_run, batch=batch, progress=sweep_progress)