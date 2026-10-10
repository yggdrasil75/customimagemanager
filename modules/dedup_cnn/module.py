"""! @file
@brief Advanced dedup scorer - learned CNN (images + video).
======================================================================
Registers a higher-priority pair-scorer that uses small learned CNNs to
judge whether two images (DupCNN) or two videos (DupVideoCNN) are the same
asset. The image model is the HEURDU size series (huggingface.co/yggdrasil75/HEURDU),
offered as the "dedup.pair" capability in Settings > Models and downloaded
on first use; a size trained locally with Trainer > Dedup overrides it.
Needs torch; when it's absent the scorer reports unavailable and dedup falls
through to the heuristic or naive score. No automatic retraining: the
merge / "not a duplicate" decisions are still recorded (dup_cnn_samples) for
Trainer > Dedup to use.

Priority is above the simple heuristic, so when trained CNNs exist they win
the pair decision; otherwise score() returns None and the next scorer (or
naive) handles the pair.

HEURDU 1.0: animations (<= 30 frames, ctx kind "anim") are scored by the same
model through DupCNN.score_animation. Settings > Models > HEURDU "Release"
picks 0.9 (default, the released checkpoints) or 1.0 (checkpoints with the
temporal block: models/heurdu1_<size>.pt from Trainer > Dedup, or the HF
HEURDU_1.0_<size>.pt). With 1.0 picked but no 1.0 checkpoint anywhere, the
0.9 one keeps answering. Longer video (kind "video") stays with the legacy
3D clip model here until HEURDUV (modules/dedup_cnn_video) answers first.
"""

import os

import common

from . import dup_cnn as _cnn_mod
from . import dup_cnn_video as _vid_mod

MANIFEST = {
    "id":          "dedup_cnn",
    "name":        "Advanced heuristic duplicates (CNN)",
    "version":     "1.0.0",
    "description": "Learned CNN duplicate scorers for images and video. Higher "
                   "accuracy on hard near-dupes; needs torch.",
    "core":        False,
    "requires":    ["dedup"],
    "pip":         ["torch"],
    "pip_optional": ["opencv-python-headless:cv2"],
    "assets":      [],
}


PRETRAINED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pretrained")   # trainer "Ship"
HF_REPO = "yggdrasil75/HEURDU"
HF_DEFAULT = HF_REPO + "/HEURDU_{size}.pt"         # owner/repo/path-in-repo, {size} substituted
HF_SIZES = ["nano", "small", "medium", "large"]     # what the repo ships
HF_DEFAULT_V1 = HF_REPO + "/HEURDU_1.0_{size}.pt"   # HEURDU 1.0 (temporal) release
RELEASES = ["0.9", "1.0"]


def register(host):
    scorers = host.get_service("dedup_scorers")
    if scorers is None:
        host.logger.info("dedup_cnn: dedup registry unavailable; skipping")
        return

    models_dir = os.path.abspath(host.core.models_dir)
    vid_path = os.path.join(models_dir, "dup_cnn_video.pt")
    vid_cnn = _vid_mod.DupVideoCNN.load(vid_path, 1.0)

    # The image model is a capability: HEURDU (the shipped size series on
    # HuggingFace) is picked in Settings > Models like any other model. A size
    # the user trained themselves (Trainer > Dedup writes models/dup_cnn_<size>.pt)
    # takes precedence over the download of the same name.
    host.declare_capability("dedup.pair", label="Duplicate pair scorer",
                            summary="Probability that two images are the same asset (0..1).",
                            input="two BGR images", output="float 0..1")
    loaded, failed = {}, {}
    host.add_config_key("dup_cnn_max_mp", default=16, validate=lambda v: max(1, int(v or 16)))
    host.add_config_key("dup_cnn_anim_max_mp", default=64, validate=lambda v: max(1, int(v or 64)))
    host.add_config_key("heurdu_release", default="0.9",
                        validate=lambda v: str(v) if str(v) in RELEASES else "0.9")

    def _release():
        return str(host.config.get("heurdu_release") or "0.9")

    def _key(size):
        return f"{_release()}:{size}"     # a release switch in Settings loads the other checkpoint

    def _v1_paths(size):
        return [os.path.join(models_dir, f"heurdu1_{size}.pt"),
                os.path.join(PRETRAINED_DIR, f"heurdu1_{size}.pt"),
                os.path.join(models_dir, "heurdu", f"heurdu1_{size}.pt")]

    def _local_paths(size):
        """! @brief Where a trained checkpoint for `size` can live, in priority order:
        Trainer > Dedup install (models/), then its "Ship" output (pretrained/),
        then a previous HEURDU download."""
        return [os.path.join(models_dir, f"dup_cnn_{size}.pt"),
                os.path.join(PRETRAINED_DIR, f"dup_cnn_{size}.pt"),
                os.path.join(models_dir, "heurdu", f"dup_cnn_{size}.pt")]

    def _local_sizes():
        out = []
        for d in (models_dir, PRETRAINED_DIR):
            try:
                for f in sorted(os.listdir(d)):
                    if f.startswith("dup_cnn_") and f.endswith(".pt") and not f.endswith(".ckpt.pt") \
                            and f != "dup_cnn_video.pt":
                        out.append(f[8:-3])
                    elif f.startswith("heurdu1_") and f.endswith(".pt"):      # HEURDU 1.0 (Trainer > Dedup)
                        out.append(f[8:-3])
            except OSError:
                pass
        return out

    def _all_sizes():
        """! @brief HEURDU sizes + the trainer's size table + anything trained on disk,
        so a size trained here (xl, xxl, a custom name) is selectable."""
        table = list(_cnn_mod.parse_sizes(host.config.get("dup_cnn_sizes")))
        return list(dict.fromkeys(HF_SIZES + table + _local_sizes()))

    def _path_for(size):
        if _release() == "1.0":
            for p in _v1_paths(size):
                if os.path.exists(p):
                    return p
            if size in HF_SIZES:
                spec = HF_DEFAULT_V1.format(size=size).strip("/")
                owner, repo, *rest = spec.split("/")
                try:
                    return common.fetch_file(f"https://huggingface.co/{owner}/{repo}/resolve/main/{'/'.join(rest)}",
                                             _v1_paths(size)[2], min_bytes=1024)
                except Exception as e:
                    host.logger.info(f"dedup_cnn: no HEURDU 1.0 {size} yet ({e}); using 0.9")
        for p in _local_paths(size):
            if os.path.exists(p):
                return p
        if size not in HF_SIZES:
            raise RuntimeError(f"no trained checkpoint for size '{size}' (looked in "
                               + ", ".join(_local_paths(size)[:2]) + "); train it in Trainer > Dedup")
        spec = HF_DEFAULT.format(size=size).strip("/")
        owner, repo, *rest = spec.split("/")
        url = f"https://huggingface.co/{owner}/{repo}/resolve/main/{'/'.join(rest)}"
        return common.fetch_file(url, os.path.join(models_dir, "heurdu", f"dup_cnn_{size}.pt"), min_bytes=1024)

    def _size():
        return str(host.model_variant("dedup.pair")["size"] or "medium")

    def _loader(cap="dedup.pair"):
        size = _size()
        m = loaded.get(_key(size))
        if m is None:
            path = _path_for(size)
            m = _cnn_mod.DupCNN.load(path)
            if not m.trained:
                raise RuntimeError(f"HEURDU {size}: checkpoint {path} did not load: {m.error or 'unknown error'}")
            loaded[_key(size)] = m
            host.logger.info(f"dedup_cnn: loaded HEURDU {m.version} {size} from {path} "
                             f"(width {m.width_mult}, depth {m.depth}, {m.params} params)")
        return lambda a, b: m.predict(a, b, _device())

    def _device():
        return "cuda" if _cnn_mod.torch.cuda.is_available() else "cpu"

    host.provide_model("dedup.pair", "heurdu", label="HEURDU", family="HEURDU",
                       sizes=_all_sizes(), loader=_loader,
                       available=lambda: bool(_cnn_mod._HAVE_TORCH),
                       reason="needs torch", cost_mb=64, gpu=False, supports_conf=False,
                       settings=[{"key": "dup_cnn_max_mp", "label": "Strip size (megapixels)", "kind": "number",
                                  "help": "Images are compared at native resolution; larger ones are encoded in "
                                          "overlapping strips of about this many megapixels to bound memory "
                                          "(~1.5 GB per 16 MP at medium). Result is identical."},
                                 {"key": "heurdu_release", "label": "Release", "kind": "select",
                                  "options": [{"value": r, "label": r + (" (animation-tuned)" if r == "1.0" else "")}
                                              for r in RELEASES],
                                  "help": "0.9: the released image model (also scores animations frame by frame). "
                                          "1.0: adds the temporal block for animations (<= 30 frames); uses "
                                          "models/heurdu1_<size>.pt when trained, else falls back to 0.9."},
                                 {"key": "dup_cnn_anim_max_mp", "label": "Animation budget (megapixels per clip)",
                                  "kind": "number",
                                  "help": "Frames x pixels of one animation encoded at once; longer / larger "
                                          "animations are scaled down to fit."}],
                       note="Siamese CNN duplicate scorer, trained on public photo sets. nano/small for a Pi, "
                            "medium for most, large+ if you have the GPU. Sizes download from "
                            "huggingface.co/" + HF_REPO + " on first use.")

    def _img_model():
        """! @brief The DupCNN for the selected size (loaded through the broker).
        Raises with the reason when there is none, so dedup can say why."""
        size = _size()
        until, why = failed.get(size, (0, ""))
        if until > _cnn_mod.time.time():
            raise RuntimeError(why)                   # said so already; retry in a while, not per pair
        m = loaded.get(_key(size))
        if m is not None:
            return m
        try:
            host.request_model("dedup.pair")
        except Exception as e:
            why = f"no image model for size '{size}': {type(e).__name__}: {e}"
            failed[size] = (_cnn_mod.time.time() + 600, why)
            host.logger.warning(f"dedup_cnn: {why} (check Settings > Models > HEURDU; retrying in 10 min)")
            raise RuntimeError(why)
        m = loaded.get(_key(size))
        if m is None:
            raise RuntimeError(f"model for size '{size}' was requested but not registered as loaded")
        return m

    def _available():
        return bool(_cnn_mod._HAVE_TORCH)

    def _score(ctx):
        return _score_batch([ctx])[0]

    def _ckpt_stamp(size):
        """! @brief mtime of the checkpoint a size loads from, so the verdict cache key
        changes when the weights are retrained (same size, new file)."""
        for p in (_v1_paths(size) if _release() == "1.0" else []) + _local_paths(size):
            if os.path.exists(p):
                return str(int(os.path.getmtime(p)))
        return "0"

    def _tag():
        size = _size()
        m = loaded.get(_key(size))
        rel = f":v{m.version}" if (m is not None and m.temporal) else ""
        return f"cnn:{size}:{_ckpt_stamp(size)}{rel}"

    def _max_px():
        return int(host.config.get("dup_cnn_max_mp", 16)) * 1_000_000

    def _score_group(imgs):
        """! @brief NxN matrix for a group of BGR images: encode once, compare many, native resolution."""
        return _img_model().score_group(imgs, _device(), _max_px())[0]

    def _score_batch(ctxs):
        """! @brief Pairwise contract: image pairs one at a time (align + encode +
        compare); video pairs through the clip model. Groups should use
        score_group instead, this is the fallback."""
        out = [None] * len(ctxs)
        m, m_err = None, None
        if any(c.get("kind") != "audio" and (not c.get("is_video") or c.get("kind") == "anim") for c in ctxs):
            try:
                m = _img_model()
            except Exception as e:
                m_err = e
        errs = []
        for i, c in enumerate(ctxs):
            if c.get("kind") == "audio":
                continue
            if c.get("kind") == "anim":
                # HEURDU animation: <= 30 frames each, native resolution.
                ra, oa = c.get("ref_anim"), c.get("other_anim")
                if m is not None and ra is not None and oa is not None and len(ra) and len(oa):
                    try:
                        out[i] = m.score_animation(list(ra), list(oa), _device(), _max_px(),
                                                   int(host.config.get("dup_cnn_anim_max_mp", 64)) * 1_000_000)
                    except Exception as e:
                        errs.append(e)
                continue
            if c.get("is_video"):
                rf, of = c.get("ref_frames"), c.get("other_frames")
                if rf and of and vid_cnn and vid_cnn.available and vid_cnn.trained:
                    try:
                        out[i] = vid_cnn.predict(rf, of)
                    except Exception as e:
                        errs.append(e)
                continue
            a, b = c.get("ref_bgr"), c.get("other_bgr")
            if m is not None and a is not None and b is not None:
                try:
                    out[i] = float(m.score_group([a, b], _device(), _max_px())[0][0, 1])
                except Exception as e:
                    errs.append(e)
        if all(p is None for p in out):
            e = m_err or (errs[0] if errs else None)
            if e is not None:
                raise e                               # nothing answered: let the registry record why
        elif errs:
            host.logger.warning(f"dedup_cnn: {len(errs)} pair(s) failed: {type(errs[0]).__name__}: {errs[0]}")
        return out

    def _change_map(a, b):
        """! @brief For the UI: (score, change map [H/8,W/8] 0..1 in a's frame, b warped
        onto a, overlap mask) or None when there is no model / no alignment."""
        try:
            m = _img_model()
        except Exception:
            return None
        r = _cnn_mod.align(a, b)
        if r is None:
            return None
        warped, ov = r
        fa, fb = m.encode(a, _device(), _max_px()), m.encode(warped, _device(), _max_px())
        cm = m.compare(fa, fb)
        return _cnn_mod.pair_score(cm, ov), cm, warped, ov

    scorers.register({
        "id": "cnn", "label": "Advanced CNN", "available": _available,
        "priority": 20, "score": _score, "score_batch": _score_batch, "score_group": _score_group,
        "tag": _tag,
        "clip_t": _vid_mod.CLIP_T,
        "kinds": ("image", "anim", "video"),
    })

    def _reload(active=None):
        """! @brief Forget loaded checkpoints so the next pair picks up a size the
        user just trained (Trainer > Dedup) or re-picked in Settings. Newly
        trained sizes become selectable; `active` (the trainer's active size)
        becomes the live pick and is persisted."""
        loaded.clear()
        failed.clear()
        try:
            prov = host.broker._providers.get("dedup.pair", {}).get("heurdu")
            if prov is not None:
                prov.sizes = _all_sizes()
        except Exception as e:
            host.logger.warning(f"dedup_cnn: refresh sizes: {e}")
        if active:
            ok, err = host.broker.select("dedup.pair", "heurdu", str(active))
            if ok:
                try:
                    host.persist_model_selection()
                except Exception as e:
                    host.logger.warning(f"dedup_cnn: save selection: {e}")
                host.logger.info(f"dedup_cnn: live size is now '{active}'")
            else:
                host.logger.warning(f"dedup_cnn: could not select size '{active}': {err}")
        return True

    def _status():
        size = host.model_variant("dedup.pair")["size"]
        m = loaded.get(_key(size))
        return {"available": bool(_cnn_mod._HAVE_TORCH), "trained": bool(m and m.trained),
                "error": (failed.get(size) or (0, ""))[1],
                "size": size or "", "params": getattr(m, "params", 0) if m else 0}

    host.provide_service("dedup_cnn", {
        "reload": _reload, "video": vid_cnn, "status": _status,
        "sizes": lambda: _cnn_mod.parse_sizes(host.config.get("dup_cnn_sizes")),
        "clip_t": _vid_mod.CLIP_T,
        "encode_pair": _cnn_mod.encode_pair,
        "change_map": _change_map,
        "encode_clip_pair": _vid_mod.encode_pair,
        "image_model": _img_model, "release": _release,
    })
    host.logger.info("dedup_cnn: registered CNN scorer; HEURDU provides dedup.pair")