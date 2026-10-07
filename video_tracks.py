"""! @file
@brief Time-indexed boxes for videos, kept in a `<video>.tracks.json` sidecar.

A track is one subject across the clip, stored as sparse keyframes
{t, cx, cy, w, h[, outside]} in seconds and normalised centre-form
coordinates. Between keyframes the box is interpolated linearly; a keyframe
marked `outside` starts a gap that lasts until the next keyframe. A track is
visible only between its first and last keyframe.
"""
from __future__ import annotations

import json
import os
import uuid

BOX_KEYS = ("cx", "cy", "w", "h")

def sidecar_path(video_path: str) -> str:
    """! @brief The sidecar path of a video."""
    return os.path.splitext(video_path)[0] + ".tracks.json"

def load(video_path: str) -> dict:
    """! @brief Read a video's tracks document.
    @return {"version": 1, "tracks": [...]}; empty when missing or unreadable.
    """
    p = sidecar_path(video_path)
    if not os.path.exists(p):
        return {"version": 1, "tracks": []}
    try:
        with open(p, encoding="utf-8") as fh:
            doc = json.load(fh)
        if not isinstance(doc, dict):
            return {"version": 1, "tracks": []}
        doc.setdefault("version", 1)
        doc["tracks"] = [_clean_track(t) for t in doc.get("tracks", [])
                         if isinstance(t, dict)]
        return doc
    except Exception:
        return {"version": 1, "tracks": []}

def save(video_path: str, doc: dict) -> dict:
    """! @brief Validate and write a tracks document; an empty one deletes the sidecar.
    @return the document as written.
    """
    tracks = [_clean_track(t) for t in (doc or {}).get("tracks", [])
              if isinstance(t, dict)]
    tracks = [t for t in tracks if t["keyframes"]]
    out = {"version": 1, "tracks": tracks}
    p = sidecar_path(video_path)
    if not tracks:
        if os.path.exists(p):
            try: os.remove(p)
            except OSError: pass
        return out
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
    return out

def _clean_track(t: dict) -> dict:
    kfs = []
    for k in t.get("keyframes", []):
        try:
            kf = {"t": float(k["t"]),
                  "cx": _clamp(k["cx"]), "cy": _clamp(k["cy"]),
                  "w": _clamp(k["w"]), "h": _clamp(k["h"])}
        except (KeyError, TypeError, ValueError):
            continue
        if k.get("outside"):
            kf["outside"] = True
        kfs.append(kf)
    kfs.sort(key=lambda k: k["t"])
    return {
        "id": str(t.get("id") or ("t_" + uuid.uuid4().hex[:8])),
        "label": str(t.get("label", "")).strip(),
        "class_name": str(t.get("class_name", "object")).strip() or "object",
        # Manual boxes are confirmed; detector proposals wait for the user.
        "confirmed": bool(t.get("confirmed", True)),
        "keyframes": kfs,
    }

def _clamp(v) -> float:
    v = float(v)
    return 0.0 if v < 0 else 1.0 if v > 1 else v

def box_at(track: dict, t: float) -> dict | None:
    """! @brief A track's interpolated box at time `t`.
    @return the box, or None when the subject is not on screen.
    """
    kfs = track.get("keyframes") or []
    if not kfs or t < kfs[0]["t"] or t > kfs[-1]["t"]:
        return None

    prev = None  # last keyframe at or before t
    nxt = None  # first keyframe after t
    for k in kfs:
        if k["t"] <= t:
            prev = k
        else:
            nxt = k
            break

    if prev is None:
        return None
    if prev.get("outside"):
        return None
    if nxt is None or prev["t"] == t:
        return {k: prev[k] for k in BOX_KEYS}

    span = nxt["t"] - prev["t"]
    f = 0.0 if span <= 0 else (t - prev["t"]) / span
    return {k: prev[k] + (nxt[k] - prev[k]) * f for k in BOX_KEYS}

def boxes_at(doc: dict, t: float) -> list[dict]:
    """! @brief Every visible box at time `t`, with its track id, label and class."""
    out = []
    for tr in doc.get("tracks", []):
        b = box_at(tr, t)
        if b is not None:
            out.append({"track_id": tr["id"], "label": tr.get("label", ""),
                        "class_name": tr.get("class_name", "object"),
                        "confirmed": tr.get("confirmed", True), **b})
    return out

def labels(doc: dict) -> list[str]:
    """! @brief Distinct non-empty labels across all tracks."""
    seen, out = set(), []
    for tr in doc.get("tracks", []):
        lb = (tr.get("label") or "").strip()
        if lb and lb.lower() not in seen:
            seen.add(lb.lower()); out.append(lb)
    return out