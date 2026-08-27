"""video_tracks.py — time-indexed bounding boxes for videos, stored as a sidecar.

The video file itself is never modified. Annotations live in a JSON sidecar next
to it (``<video-basename>.tracks.json``), travelling with the asset through the
same move/delete plumbing as .txt/.xmp sidecars.

A "track" is one tagged subject (a person, usually) that persists across the clip.
Instead of a box on every frame, a track carries sparse KEYFRAMES and everything
between them is linearly interpolated — the same model CVAT/Label-Studio use:

    track = {
        "id": "t_ab12",
        "label": "Alice",              # the person's name → your tag
        "class_name": "person",
        "keyframes": [                 # sorted by t (seconds)
            {"t": 3.2, "cx":.4, "cy":.5, "w":.1, "h":.3},
            {"t": 5.0, "cx":.5, "cy":.5, "w":.1, "h":.3, "outside": True},
            {"t": 8.0, "cx":.2, "cy":.5, "w":.1, "h":.3},
        ],
    }

Semantics of ``boxes_at(t)``:
  • A track is visible only within [first keyframe t, last keyframe t].
  • Between two keyframes we lerp cx/cy/w/h by time fraction …
  • … unless the earlier keyframe is ``outside`` — then that span is a gap
    (subject not on screen), so nothing is drawn until the next keyframe.
  • Exactly on a keyframe returns that box (unless it's ``outside``).

Coordinates are normalized 0-1 (cx, cy = box centre; w, h = size), identical to
the image region model, so the same overlay math draws both.
"""
from __future__ import annotations

import json
import os
import uuid

BOX_KEYS = ("cx", "cy", "w", "h")

def sidecar_path(video_path: str) -> str:
    """`/media/clip.mp4` → `/media/clip.tracks.json`."""
    return os.path.splitext(video_path)[0] + ".tracks.json"

# ── load / save ───────────────────────────────────────────────────────────────
def load(video_path: str) -> dict:
    """Return the tracks document (``{"version":1,"tracks":[...]}``). A missing or
    unreadable sidecar yields an empty document rather than raising."""
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
    """Validate and write the document. Empty tracks are dropped; if nothing is
    left the sidecar is deleted so we don't litter empty files. Returns the
    cleaned document that was written."""
    tracks = [_clean_track(t) for t in (doc or {}).get("tracks", [])
              if isinstance(t, dict)]
    tracks = [t for t in tracks if t["keyframes"]]        # drop empty tracks
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
        # Confirmation parity with image regions: manual boxes are confirmed,
        # YOLO proposals arrive unconfirmed until the user accepts them.
        "confirmed": bool(t.get("confirmed", True)),
        "keyframes": kfs,
    }

def _clamp(v) -> float:
    v = float(v)
    return 0.0 if v < 0 else 1.0 if v > 1 else v

# ── interpolation ─────────────────────────────────────────────────────────────
def box_at(track: dict, t: float) -> dict | None:
    """The interpolated box for one track at time ``t``, or None if the subject
    isn't on screen then."""
    kfs = track.get("keyframes") or []
    if not kfs or t < kfs[0]["t"] or t > kfs[-1]["t"]:
        return None

    prev = None       # last keyframe at or before t
    nxt = None        # first keyframe strictly after t
    for k in kfs:
        if k["t"] <= t:
            prev = k
        else:
            nxt = k
            break

    if prev is None:
        return None
    if prev.get("outside"):
        return None                       # inside a declared gap
    if nxt is None or prev["t"] == t:
        # exactly on prev (or prev is the final keyframe)
        return {k: prev[k] for k in BOX_KEYS}

    span = nxt["t"] - prev["t"]
    f = 0.0 if span <= 0 else (t - prev["t"]) / span
    return {k: prev[k] + (nxt[k] - prev[k]) * f for k in BOX_KEYS}

def boxes_at(doc: dict, t: float) -> list[dict]:
    """Every visible box at time ``t`` across all tracks, each annotated with its
    track id / label / class — ready to hand to an overlay renderer."""
    out = []
    for tr in doc.get("tracks", []):
        b = box_at(tr, t)
        if b is not None:
            out.append({"track_id": tr["id"], "label": tr.get("label", ""),
                        "class_name": tr.get("class_name", "object"),
                        "confirmed": tr.get("confirmed", True), **b})
    return out

def labels(doc: dict) -> list[str]:
    """Distinct non-empty person labels in the document (for tagging / search)."""
    seen, out = set(), []
    for tr in doc.get("tracks", []):
        lb = (tr.get("label") or "").strip()
        if lb and lb.lower() not in seen:
            seen.add(lb.lower()); out.append(lb)
    return out