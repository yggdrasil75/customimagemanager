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

    host.provide_model(
        "pose", "mp_blazepose", label="BlazePose", family="MediaPipe",
        sizes=["lite", "full", "heavy"],
        types=[{"value": "blazepose", "label": "BlazePose · 33 pts"}],
        note="Google BlazePose landmarker: 33 body points incl. face outline, hands and feet. "
             "Runs on CPU; per-person crops when a person detector is picked.",
        speed="fast",
        loader=lambda: (lambda sz: (lambda img, *a, **k: _people(img, "pose", sz, _persons())))(
            host.model_variant("pose")["size"] or "full"),
        transform=None, available=lambda: _HAVE_MP, reason="pip install mediapipe",
        cost_mb=60)

    host.provide_model(
        "pose", "mp_holistic", label="Holistic", family="MediaPipe", sizes=[],
        types=[{"value": "holistic", "label": "Holistic · 543 (face mesh + hands)"}],
        note="MediaPipe Holistic: BlazePose 33 + 468-point face mesh + two 21-point hands.",
        speed="balanced",
        loader=lambda: (lambda img, *a, **k: _people(img, "holistic", "", _persons())),
        transform=None, available=lambda: _HAVE_MP, reason="pip install mediapipe",
        cost_mb=300)

    host.logger.info("mediapipe_pose module: registered mp_blazepose (33) and mp_holistic (543)")