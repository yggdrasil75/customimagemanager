"""
MediaPipe pose provider (BlazePose 33 / Holistic 543).
======================================================================
Registers Google MediaPipe's Tasks landmarkers as 'pose' providers:

  mp_blazepose  PoseLandmarker  33 keypoints  sizes lite / full / heavy
  mp_holistic   HolisticLandmarker 543 keypoints (33 pose + 468 face mesh
                + 21 left hand + 21 right hand), one size

Both landmarkers are single-person by design, so when the app has a
'detect.persons' pick each person box is cropped and landmarked on its own
(the same top-down trick RTMPose uses); with no person detector the whole
image is run once. The .task bundles download on first use into
models/mediapipe/pose/.
"""

import os

import numpy as np

from optional_deps import optional_import
import model_registry
import common

mp, _HAVE_MP = optional_import("mediapipe")

MANIFEST = {
    "id":          "mediapipe_pose",
    "name":        "MediaPipe pose",
    "version":     "1.0.0",
    "description": "MediaPipe BlazePose (33 pts) and Holistic (543 pts: body + face mesh "
                   "+ hands) as pose providers. CPU-friendly.",
    "core":        False,
    "requires":    [],
    "pip":         ["mediapipe"],
    "assets":      [],
}

_BASE = "https://storage.googleapis.com/mediapipe-models/"
_POSE_URL = _BASE + "pose_landmarker/pose_landmarker_{s}/float16/latest/pose_landmarker_{s}.task"
_HOLISTIC_URL = _BASE + "holistic_landmarker/holistic_landmarker/float16/latest/holistic_landmarker.task"
_REGISTERED = set()


def _task(url):
    return common.fetch_file(url, os.path.join(model_registry.model_dir("mediapipe", "pose"),
                                               os.path.basename(url)))


def _mp_image(crop_bgr):
    from mediapipe.tasks.python.vision.core.image import Image, ImageFormat
    return Image(ImageFormat.SRGB, np.ascontiguousarray(crop_bgr[:, :, ::-1]))


def _lm(landmarks, n):
    """[(x, y, v)] from a NormalizedLandmark list, padded to n when missing."""
    out = [(l.x, l.y, l.visibility if l.visibility is not None else
            (l.presence if l.presence is not None else 1.0)) for l in (landmarks or [])]
    return out + [(0.0, 0.0, 0.0)] * (n - len(out))


def _build(kind, size):
    from mediapipe.tasks.python.core.base_options import BaseOptions
    from mediapipe.tasks.python import vision
    if kind == "holistic":
        opts = vision.HolisticLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=_task(_HOLISTIC_URL)),
            running_mode=vision.RunningMode.IMAGE,
            output_face_blendshapes=False,
            output_segmentation_mask=False)
        # output_segmentation_mask=False does not stop this mediapipe build
        # from running SegmentationSmoothingCalculator, which keeps the previous
        # frame's mask and RET_CHECKs the next frame is the same size. Person
        # crops differ in size, so a graph may only ever see one size: rebuild
        # it whenever the crop size changes (cheap next to a detect).
        state = {"lm": None, "shape": None}

        def _lm_for(crop):
            shape = tuple(crop.shape[:2])
            if state["lm"] is None or state["shape"] != shape:
                if state["lm"] is not None:
                    try:
                        state["lm"].close()
                    except Exception:
                        pass
                state["lm"] = vision.HolisticLandmarker.create_from_options(opts)
                state["shape"] = shape
            return state["lm"]

        def run(crop):
            r = _lm_for(crop).detect(_mp_image(crop))
            if not r.pose_landmarks:
                return []
            return [_lm(r.pose_landmarks, 33) + _lm(r.face_landmarks, 468)
                    + _lm(r.left_hand_landmarks, 21) + _lm(r.right_hand_landmarks, 21)]
    else:
        opts = vision.PoseLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=_task(_POSE_URL.format(s=size))),
            num_poses=1)
        lm = vision.PoseLandmarker.create_from_options(opts)

        def run(crop):
            return [_lm(p, 33) for p in lm.detect(_mp_image(crop)).pose_landmarks]
    return run


def _load(kind, size):
    key = f"pose:mediapipe:{kind}:{size}"
    if key not in _REGISTERED:
        model_registry.register(key, lambda: _build(kind, size),
                                cost_mb=300 if kind == "holistic" else 60, gpu=False)
        _REGISTERED.add(key)
    return model_registry.acquire(key)


# ── MediaPipe -> COCO conversion (this provider's business, nobody else's) ──
# BlazePose-33 index for each COCO-17 point.
BLAZE_TO_COCO17 = [0, 2, 5, 7, 8, 11, 12, 13, 14, 15, 16, 23, 24, 25, 26, 27, 28]
# COCO-WholeBody feet (l_big_toe, l_small_toe, l_heel, r_big_toe, r_small_toe,
# r_heel) from foot_index / heel (BlazePose has no separate small toe).
BLAZE_TO_WB_FEET = [31, 31, 29, 32, 32, 30]
# 68 iBUG landmarks picked out of the 468-point face mesh
# (jaw 17, brows 10, nose 9, eyes 12, mouth 20).
MESH468_TO_68 = [162, 234, 93, 58, 172, 136, 149, 148, 152, 377, 378, 365, 397, 288, 323, 454, 389,
                 71, 63, 105, 66, 107, 336, 296, 334, 293, 301,
                 168, 197, 5, 4, 75, 97, 2, 326, 305,
                 33, 160, 158, 133, 153, 144, 362, 385, 387, 263, 373, 380,
                 61, 39, 37, 0, 267, 269, 291, 405, 314, 17, 84, 181,
                 78, 82, 13, 312, 308, 317, 14, 87]
_HOL_FACE, _HOL_LHAND, _HOL_RHAND = 33, 501, 522


def to_coco(keypoints):
    """33 (BlazePose) -> 17 COCO; 543 (Holistic) -> 133 COCO-WholeBody; other
    counts (already converted, or empty) pass through. Same {x,y,v} dicts."""
    n = len(keypoints)
    if n == 33:
        return [keypoints[i] for i in BLAZE_TO_COCO17]
    if n == 543:
        return ([keypoints[i] for i in BLAZE_TO_COCO17] + [keypoints[i] for i in BLAZE_TO_WB_FEET]
                + [keypoints[_HOL_FACE + i] for i in MESH468_TO_68]
                + keypoints[_HOL_LHAND:_HOL_LHAND + 21] + keypoints[_HOL_RHAND:_HOL_RHAND + 21])
    return keypoints


def _people(img_bgr, kind, size, persons):
    img = common.coerce_bgr(img_bgr)
    if img is None:
        return []
    run = _load(kind, size)
    if run is None:
        raise RuntimeError(f"MediaPipe {kind} failed to load")
    H, W = img.shape[:2]
    out = []
    for crop, x0, y0, w, h in common.person_crops(img, persons):
        for pts in run(crop):
            out.append({"keypoints": common.crop_keypoints(pts, x0, y0, w, h, W, H), "conf": 1.0})
    return out


def register(host):
    from modules.model_broker import NoProviderError

    def _persons():
        try:
            det = host.request_model("detect.persons")
        except NoProviderError:
            return None
        return lambda img: det(img, conf=0.25)

    # Downsample to the app's COCO topologies (33 -> 17, 543 -> 133) so every
    # consumer (drawing, t-pose, learners) sees one skeleton family. Off = native
    # skeletons are stored; learners still get COCO tokens through the
    # pose.tokens.<id> services below, which convert then normalise.
    host.add_config_key("mp_pose_coco", default=True, validate=lambda v: bool(v))
    def _coco(raw, *a, **k):
        if not host.config.get("mp_pose_coco", True):
            return raw
        return [dict(p, keypoints=to_coco(p.get("keypoints") or [])) for p in raw or []]
    coco_setting = {"key": "mp_pose_coco", "label": "Downsample to COCO 17 / 133", "kind": "toggle"}

    def _tokens(keypoints):
        from modules.pose import skeleton
        return skeleton.tokens(to_coco(keypoints))
    host.provide_service("pose.tokens.mp_blazepose", _tokens)
    host.provide_service("pose.tokens.mp_holistic", _tokens)

    host.provide_model(
        "pose", "mp_blazepose", label="BlazePose", family="MediaPipe",
        sizes=["lite", "full", "heavy"],
        types=[{"value": "blazepose", "label": "BlazePose · 33 pts (17 when downsampled)"}],
        note="Google BlazePose landmarker: 33 body points incl. face outline, hands and feet. "
             "Runs on CPU; per-person crops when a person detector is picked.",
        speed="fast",
        loader=lambda: (lambda sz: (lambda img, *a, **k: _people(img, "pose", sz, _persons())))(
            host.model_variant("pose")["size"] or "full"),
        transform=_coco, settings=[coco_setting],
        available=lambda: _HAVE_MP, reason="pip install mediapipe",
        cost_mb=60)

    host.provide_model(
        "pose", "mp_holistic", label="Holistic", family="MediaPipe", sizes=[],
        types=[{"value": "holistic", "label": "Holistic · 543 (133 whole-body when downsampled)"}],
        note="MediaPipe Holistic: BlazePose 33 + 468-point face mesh + two 21-point hands.",
        speed="balanced",
        loader=lambda: (lambda img, *a, **k: _people(img, "holistic", "", _persons())),
        transform=_coco, settings=[coco_setting],
        available=lambda: _HAVE_MP, reason="pip install mediapipe",
        cost_mb=300)

    host.logger.info("mediapipe_pose module: registered mp_blazepose (33) and mp_holistic (543)")