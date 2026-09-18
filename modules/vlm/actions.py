"""
AI actions — named prompts the user runs against an image (or many).
======================================================================
Each action is {id, name, prompt, target}; the target decides how the
model's answer lands in the file: description (append), tags (merge as
unconfirmed), regions (boxes, unconfirmed), flag (review queue), body
(person record fields via the people module), or any target another
module registered with host.register_action_target (segmentation).
"""
import os

from flask import request, jsonify

from . import client

HOST = None

DEFAULT_ACTIONS = [
    {"id": "1", "name": "Describe Scene", "prompt": "Describe the overall scene, lighting, and composition in a detailed paragraph.", "target": "description"},
    {"id": "2", "name": "Describe Clothes", "prompt": "Focus entirely on the subject's clothing, style, and accessories.", "target": "description"},
    {"id": "3", "name": "Booru Tags", "prompt": "Generate a comma-separated list of Danbooru-style tags for the subjects and scene.", "target": "tags"},
    {"id": "4", "name": "Box Objects", "prompt": "Identify the main objects in the image and draw bounding boxes around them.", "target": "regions"},
    {"id": "5", "name": "Flag if bad", "prompt": "Assess this image: is it blurry, corrupted, a duplicate-looking screenshot, or otherwise low quality?", "target": "flag"},
]


def _action(action_id):
    return next((a for a in HOST.config.get("oai_actions", []) if str(a["id"]) == str(action_id)), None)


def apply_body(rel, bgr, action):
    """Fill the fixed body-description slots for each identified person in an
    image (people module's BODY_FIELDS) from one JSON answer."""
    people = HOST.get_service("people")
    if not people:
        return True
    clusters = people["clusters_in_image"](rel)
    if not clusters:
        return True
    fields = ", ".join(people["BODY_FIELDS"])
    prompt = (action.get("prompt", "") +
              f"\n\nDescribe the person. Respond ONLY as JSON with these keys: {fields}. "
              "Use a short phrase per key, empty string if unknown.")
    res = client.call(prompt, bgr, "json") or {}
    for cid in clusters:
        for key in people["BODY_FIELDS"]:
            val = str(res.get(key, "")).strip()
            if val:
                people["store_person_field"](cid, "body", key, val)
    return True


def apply(fp, action):
    """Run one action against a file and merge the result into its metadata.
    Returns the regions it added (list), or True/False."""
    c = HOST.core
    target = action.get("target", "description")
    prompt = action.get("prompt", "")
    img = c.read_image(fp)
    if img is None:
        return False
    bgr = c.to_bgr(img)
    meta = c.read_metadata(fp)
    if target == "flag":
        res = client.call(prompt + '\n\nRespond ONLY as JSON: {"delete": true|false, "reason": "short reason"}',
                          bgr, "json") or {}
        delete = bool(res.get("delete"))
        c.write_metadata(fp, meta["tags"], meta["description"], meta["regions"],
                         flag={"delete": delete, "reason": str(res.get("reason", ""))[:300]})
        return True
    if target == "body":
        return apply_body(c.rel(fp), bgr, action)
    handler = HOST.action_targets.get(target)
    if handler:                      # another module's target (segmentation: "segment")
        return handler(fp, bgr, meta, action)
    if target == "regions":
        new = [{**b, "confirmed": False} for b in
               (client.call(prompt + "\n\nReturn bounding boxes normalised 0..1.", bgr, "boxes") or [])]
        if new:
            classes = HOST.config["classes"]
            for n in new:
                if n["class_name"] not in classes:
                    classes.append(n["class_name"])
            c.save_classes()
            c.write_metadata(fp, meta["tags"], meta["description"], meta["regions"] + new)
        return new
    if target == "tags":
        tags = client.call(prompt, bgr, "tags") or []
        merged = list(meta["tags"])
        seen = {c.tag_name(t).lower() for t in meta["tags"]}
        for t in tags:
            nm = c.tag_name(t)
            if nm and nm.lower() not in seen:
                merged.append(c.make_tag(nm, confirmed=False))   # AI suggestion → unconfirmed
                seen.add(nm.lower())
        c.write_metadata(fp, merged, meta["description"], meta["regions"])
        return tags
    text = (client.call(prompt, bgr, "text") or "").strip()     # description
    if text:
        desc = (meta["description"] + "\n\n" + text).strip() if meta["description"].strip() else text
        c.write_metadata(fp, meta["tags"], desc, meta["regions"])
    return text


# ── routes ────────────────────────────────────────────────────────────────────
def run_llm():
    """Run one action on one file and return the raw result for the editor to
    apply live (it is NOT written here; the editor's autosave does that)."""
    c = HOST.core
    fp = HOST.safe_path(HOST.media_dir, request.json.get("filename", ""))
    action = _action(request.json.get("action_id", ""))
    if not fp or not os.path.exists(fp):
        return jsonify({"success": False, "error": "File not found."})
    if not action:
        return jsonify({"success": False, "error": "Unknown AI action."})
    try:
        img = c.read_image(fp)
        if img is None:
            raise RuntimeError("Decode failed")
        bgr = c.to_bgr(img)
        t = action["target"]
        if t == "flag":
            res = client.call(action["prompt"] + '\n\nRespond ONLY as JSON: {"delete": true|false, "reason": "short reason"}',
                              bgr, "json") or {}
            delete, reason = bool(res.get("delete")), str(res.get("reason", ""))[:300]
            meta = c.read_metadata(fp)
            c.write_metadata(fp, meta["tags"], meta["description"], meta["regions"],
                             flag={"delete": delete, "reason": reason})
            return jsonify({"success": True, "target": "flag", "delete": delete, "reason": reason})
        if t == "body" or t in HOST.action_targets:
            res = apply(fp, action)
            return jsonify({"success": True, "target": "regions",
                            "regions": res if isinstance(res, list) else []})
        if t == "regions":
            boxes = [{**b, "confirmed": False} for b in
                     (client.call(action["prompt"] + "\n\nReturn bounding boxes normalised 0..1.", bgr, "boxes") or [])]
            classes = HOST.config["classes"]
            for b in boxes:
                if b.get("class_name") and b["class_name"] not in classes:
                    classes.append(b["class_name"])
            c.save_classes()
            return jsonify({"success": True, "target": "regions", "regions": boxes})
        if t == "tags":
            return jsonify({"success": True, "target": "tags", "tags": client.call(action["prompt"], bgr, "tags")})
        return jsonify({"success": True, "target": "description",
                        "description": client.call(action["prompt"], bgr, "text")})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


def bulk_llm():
    """Run an action on many files, writing the result into each."""
    filenames = request.json.get("filenames", [])
    action = _action(request.json.get("action_id", ""))
    if not action:
        return jsonify({"success": False, "error": "Unknown AI action."})
    done, applied, errors = 0, 0, []
    total = len(filenames)
    for fn in filenames:
        fp = HOST.safe_path(HOST.media_dir, fn)
        if not fp or not os.path.exists(fp):
            errors.append(fn); continue
        try:
            if apply(fp, action):
                applied += 1
            done += 1
            HOST.config["status_text"] = f"AI ({action.get('name', 'action')}): {done}/{total}"
        except Exception as e:
            errors.append(fn)
            HOST.logger.error(f"bulk_llm {fn}: {e}")
    HOST.config["status_text"] = "Ready."
    return jsonify({"success": True, "done": done, "applied": applied, "errors": errors})