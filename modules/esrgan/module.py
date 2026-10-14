"""! @file
@brief Upscale: AI super-resolution of library pictures with ESRGAN-family models.

Adds the `upscale` capability (Settings -> Models) with one provider, ESRGAN:

  stock     Real-ESRGAN x4plus / x2plus / general x4v3 / x4plus anime 6B /
            anime video v3, picked as the provider's type and downloaded from
            the Real-ESRGAN releases into models/esrgan/stock/ on first use;
  custom    any ESRGAN (old "model.N" layout, any factor), BasicSR / Real-ESRGAN
            RRDBNet (x1 / x2 unshuffle variants included) or compact SRVGG
            checkpoint (.pth .pt .ckpt .safetensors) dropped in
            models/esrgan/upscale/ or uploaded in Settings -> Upscale, picked as
            "Custom weights"; with spandrel installed, every architecture it
            knows (SwinIR, HAT, DAT, OmniSR, ...) loads too.

An "Upscale" button in the viewer's AI tools and the gallery bulk bar runs the
picked model on the open file or the selection: a new file next to the original
(default) or, with the "Replace originals" permission, the original replaced in
place (kept under .cim/upscale_originals/ unless turned off). See jobs.py.

Settings -> Upscale: default factor and output, tile size, half precision,
largest result, JPEG / WebP quality for replaced files, the weight files and
the recent jobs. Permissions: "Upscale pictures" and "Upscale: replace originals".
"""
import os
import threading

import requests
from flask import g, jsonify, request
from werkzeug.utils import secure_filename

import model_registry
from modules.model_broker import NoProviderError
from optional_deps import optional_import

from . import jobs as jobs_mod

torch, HAVE_TORCH = optional_import("torch")
AVAILABLE = HAVE_TORCH
UNAVAILABLE_REASON = "torch not installed"
if HAVE_TORCH:
    from . import esrgan_arch, upscaler

MANIFEST = {
    "id":          "upscale",
    "name":        "Upscale (ESRGAN)",
    "version":     "1.0.0",
    "description": "AI upscaling of pictures with ESRGAN / Real-ESRGAN models (stock or your own "
                   "weights): an Upscale button in the viewer and the gallery bulk bar.",
    "core":        False,
    "requires":    [],
    "pip":         ["torch"],
    "pip_optional": ["safetensors", "spandrel"],
    "assets":      ["upscale.js", "upscale.css"],
}

CAP = "upscale"
TAB = "upscale"
FEATURE = "ai.upscale"
FEATURE_REPLACE = "ai.upscale.replace"
WEIGHT_EXTS = (".pth", ".pt", ".ckpt", ".safetensors")
_REL = "https://github.com/xinntao/Real-ESRGAN/releases/download/"
## @brief Stock weights: type id -> (label, url, file, factor, note).
STOCK = {
    "x4plus": ("Real-ESRGAN x4plus (photos)", _REL + "v0.1.0/RealESRGAN_x4plus.pth",
               "RealESRGAN_x4plus.pth", 4, "The general photo model; slow on CPU."),
    "x2plus": ("Real-ESRGAN x2plus (photos)", _REL + "v0.2.1/RealESRGAN_x2plus.pth",
               "RealESRGAN_x2plus.pth", 2, "Photos at x2."),
    "general_x4v3": ("Real-ESRGAN general x4v3 (fast)", _REL + "v0.2.5.0/realesr-general-x4v3.pth",
                     "realesr-general-x4v3.pth", 4, "Small and quick; softer than x4plus."),
    "anime_6b": ("Real-ESRGAN x4plus anime 6B (drawings)", _REL + "v0.2.2.4/RealESRGAN_x4plus_anime_6B.pth",
                 "RealESRGAN_x4plus_anime_6B.pth", 4, "Illustrations, anime and line art."),
    "animevideo_v3": ("Real-ESRGAN anime video v3 (fast drawings)", _REL + "v0.2.5.0/realesr-animevideov3.pth",
                      "realesr-animevideov3.pth", 4, "Tiny model for drawings and cartoons."),
}
FACTORS = [{"value": "0", "label": "The model's own"}, {"value": "2", "label": "x2"},
           {"value": "3", "label": "x3"}, {"value": "4", "label": "x4"}, {"value": "8", "label": "x8"}]
MODES = [{"value": "copy", "label": "A new file next to the original"},
         {"value": "replace", "label": "Replace the original"}]

DEFAULTS = {
    "upscale_factor": "0",
    "upscale_mode": "copy",
    "upscale_keep_originals": True,
    "upscale_tile": 512,
    "upscale_half": True,
    "upscale_max_mp": 200,
    "upscale_jpeg_quality": 95,
}


def _int(lo, hi):
    def check(v):
        try:
            n = int(float(v if v not in (None, "") else 0))
        except (TypeError, ValueError):
            raise ValueError("must be a number")
        return max(lo, min(hi, n))
    return check


def _one_of(options):
    allowed = {o["value"] for o in options}

    def check(v):
        v = str(v if v is not None else "")
        if v not in allowed:
            raise ValueError("unknown value %r" % v)
        return v
    return check


def parse_factor(v, default=0.0):
    """! @brief A requested factor: 0 / "" = the model's own, else 1..16."""
    try:
        f = float(v if v not in (None, "") else default)
    except (TypeError, ValueError):
        raise ValueError("factor must be a number")
    if f and not 1.0 < f <= 16.0:
        raise ValueError("factor must be above 1 and at most 16")
    return f


def stock_dir():
    return model_registry.model_dir("esrgan", "stock")


def custom_dir():
    return model_registry.model_dir("esrgan", CAP)


def custom_weights():
    return model_registry.list_weights("esrgan", CAP, exts=WEIGHT_EXTS)


_DL_LOCK = threading.Lock()


def stock_path(type_id, log=None):
    """! @brief The stock checkpoint for a type, downloaded on first use."""
    label, url, fname, _, _ = STOCK.get(type_id) or STOCK["x4plus"]
    dest = os.path.join(stock_dir(), fname)
    with _DL_LOCK:
        if os.path.exists(dest):
            return dest
        if log:
            log.info(f"upscale: downloading {label} from {url}")
        tmp = dest + ".part"
        try:
            with requests.get(url, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
            if os.path.getsize(tmp) < 1 << 20:
                raise RuntimeError("download of %s is too small" % fname)
            os.replace(tmp, dest)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    return dest


def register(host):
    """! @brief Capability + provider, settings tab, permissions, routes and the job runner."""
    core, log = host.core, host.logger

    # -- permissions ------------------------------------------------------
    host.register_feature(FEATURE, "Upscale pictures (AI super-resolution)", section="ai_tooling",
                          section_label="AI Tooling", default="write",
                          role_defaults={"viewer": "block", "uploader": "block"})
    host.register_feature(FEATURE_REPLACE, "Upscale: replace originals in place", section="ai_tooling",
                          section_label="AI Tooling", default="block")

    # -- settings ---------------------------------------------------------
    host.add_settings_tab(TAB, "Upscale", icon="", admin_only=True, group="modules")
    validators = {
        "upscale_factor": _one_of(FACTORS),
        "upscale_mode": _one_of(MODES),
        "upscale_keep_originals": bool,
        "upscale_tile": _int(0, 4096),
        "upscale_half": bool,
        "upscale_max_mp": _int(1, 2000),
        "upscale_jpeg_quality": _int(50, 100),
    }
    for key, dflt in DEFAULTS.items():
        host.add_config_key(key, default=dflt, validate=validators[key], tab=TAB)
    fields = [
        ("upscale_factor", "Default factor", "select", {"options": FACTORS,
         "help": "Above the model's own the model runs again; the result is then resized to the exact factor."}),
        ("upscale_mode", "Default output", "select", {"options": MODES,
         "help": "Replacing needs the \"Upscale: replace originals\" permission."}),
        ("upscale_keep_originals", "Keep replaced originals in .cim/upscale_originals", "toggle", {}),
        ("upscale_tile", "Tile size (pixels, 0 = whole picture)", "number",
         {"help": "Smaller tiles use less memory; a tile that runs out of GPU memory is retried smaller."}),
        ("upscale_half", "Half precision on the GPU", "toggle", {"help": "Faster and lighter; off if results look broken."}),
        ("upscale_max_mp", "Largest result (megapixels)", "number", {}),
        ("upscale_jpeg_quality", "JPEG / WebP quality when replacing", "number", {}),
    ]
    for key, label, kind, extra in fields:
        host.add_settings_field(key=key, label=label, kind=kind, pane=TAB, **extra)
    host.add_config_key("upscale_esrgan_weights", default="", validate=lambda v: str(v or ""))

    def cfg():
        return {k: host.config.get(k, d) for k, d in DEFAULTS.items()}

    # -- capability + provider --------------------------------------------
    host.declare_capability(
        CAP, label="Upscale (super-resolution)",
        summary="Make a picture larger with detail a model fills in.",
        input="uint8 picture HxW, HxWx3 (RGB) or HxWx4; kwargs tile=, progress=(done, total)",
        output="the picture scaled by the model's factor (handle.model.scale), same channels")

    def _weights_options():
        return [{"value": "", "label": "Stock (the type above)"}] + \
               [{"value": p, "label": os.path.basename(p)} for p in custom_weights()]

    def _load():
        """! @brief The picked weights as a cached Upscaler."""
        custom = str(host.config.get("upscale_esrgan_weights") or "")
        if custom and os.path.isfile(custom):
            path = custom
        else:
            path = stock_path(host.model_variant(CAP).get("type") or "x4plus", log)
        dev = model_registry.device()
        half = bool(host.config.get("upscale_half", True)) and dev == "cuda"
        key = f"esrgan:{path}:{'fp16' if half else 'fp32'}"
        model_registry.register(key, lambda: upscaler.load_model(path, dev, half=half),
                                cost_mb=300, gpu=dev == "cuda", model_path=path)
        model = model_registry.acquire(key)
        if model is None:
            err = model_registry.error(key)
            model_registry.unload(key)     # let a fixed file load next time
            raise RuntimeError(f"could not load {os.path.basename(path)}: {err}")
        return model

    host.provide_model(
        CAP, "esrgan", label="ESRGAN / Real-ESRGAN", family="ESRGAN", speed="balanced",
        types=[{"value": k, "label": v[0]} for k, v in STOCK.items()],
        settings=[{"key": "upscale_esrgan_weights", "label": "Custom weights", "kind": "select",
                   "options": _weights_options,
                   "help": "ESRGAN-family .pth / .safetensors in models/esrgan/upscale "
                           "(or uploaded in Settings -> Upscale). Blank = the stock type."}],
        note="Stock weights download on first use; your own ESRGAN checkpoints work too.",
        loader=_load, available=lambda: HAVE_TORCH, reason=UNAVAILABLE_REASON,
        cost_mb=300, gpu=model_registry.on_gpu())

    def _drop_models(new=None, old=None):
        model_registry.clear("esrgan:")
    host.on_setting_change("upscale_half", _drop_models)

    # -- jobs -------------------------------------------------------------
    jobs = jobs_mod.Jobs(host, cfg)

    def _user():
        return (g.get("user") or {}).get("username", "")

    def _has(feature, level="write"):
        if host.is_admin():
            return True
        feats = (g.get("user") or {}).get("features") or {}
        return core.features.has_level(feats, feature, level)

    def _check_file(rel, mode):
        """! @brief Why `rel` can't be upscaled this way (None when it can)."""
        fp = host.safe_path(host.media_dir, rel) if rel else None
        if not fp or not os.path.isfile(fp) or not host.check_path(rel, write=False):
            return "file not found"
        media = host.media
        if media.kind(fp) != "image":
            return "only still pictures can be upscaled"
        anim = False
        if fp.lower().endswith(".jxl"):
            anim = bool((media.jxl_anim_info(fp) or {}).get("animated"))
        elif media.is_animated_input(fp):
            anim = True
        if anim:
            return "animations can't be upscaled"
        if mode == "replace":
            if not _has(FEATURE_REPLACE):
                return "you may not replace originals"
            if not host.check_path(rel, write=True):
                return "you may not change this file"
            if os.path.splitext(rel)[1].lower() not in jobs_mod.REPLACE_EXTS:
                return "replacing %s files is not supported" % os.path.splitext(rel)[1]
        else:
            if not _has("data.upload"):
                return "you may not add files"
            folder = os.path.dirname(rel)
            if not host.check_path((folder + "/" if folder else "") + "upscaled.png", write=True):
                return "you may not add files to this folder"
            for reason in host.emit("upload.check", folder=folder, filename="upscaled.png", size=0):
                if reason:
                    return str(reason)
            limit = core.storage_limit_reason() if hasattr(core, "storage_limit_reason") else ""
            if limit:
                return str(limit)
        return None

    def api_run():
        body = request.get_json(silent=True) or {}
        files = body.get("filenames") or ([body["filename"]] if body.get("filename") else [])
        files = [str(f).replace("\\", "/").strip("/") for f in files if str(f or "").strip()]
        if not files:
            return jsonify({"success": False, "error": "no file given"}), 400
        if len(files) > 500:
            return jsonify({"success": False, "error": "at most 500 files at a time"}), 400
        mode = str(body.get("mode") or cfg()["upscale_mode"])
        if mode not in ("copy", "replace"):
            return jsonify({"success": False, "error": "unknown mode"}), 400
        try:
            factor = parse_factor(body.get("factor"), cfg()["upscale_factor"])
        except ValueError as e:
            return jsonify({"success": False, "error": str(e)}), 400
        try:
            if not host.broker.selected_id(CAP):
                raise NoProviderError(CAP, "none_available", "no upscale model is available")
        except NoProviderError as e:
            return jsonify({"success": False, "error": str(e)}), 503
        queued, refused = [], []
        for rel in files:
            why = _check_file(rel, mode)
            if why:
                refused.append({"filename": rel, "error": why})
            else:
                queued.append(jobs.submit(rel, factor, mode, _user()))
        return jsonify({"success": bool(queued), "jobs": queued, "refused": refused,
                        **({} if queued else {"error": refused[0]["error"]})})

    def api_jobs():
        mine = None if host.is_admin() else _user()
        return jsonify({"success": True, "jobs": jobs.list(mine)})

    def api_cancel():
        jid = str((request.get_json(silent=True) or {}).get("id") or "")
        j = jobs.get(jid)
        if not j or (not host.is_admin() and j["user"] != _user()):
            return jsonify({"success": False, "error": "no such job"}), 404
        return jsonify({"success": jobs.cancel(jid)})

    def api_info():
        c = cfg()
        try:
            sel = host.broker.selected_id(CAP)
        except Exception:
            sel = None
        custom = str(host.config.get("upscale_esrgan_weights") or "")
        if custom and os.path.isfile(custom):
            model = os.path.basename(custom)
        else:
            t = host.model_variant(CAP).get("type") or "x4plus"
            model = STOCK.get(t, STOCK["x4plus"])[0]
        return jsonify({"success": True, "available": HAVE_TORCH and bool(sel), "model": model,
                        "factor": c["upscale_factor"], "mode": c["upscale_mode"], "factors": FACTORS,
                        "can_replace": _has(FEATURE_REPLACE), "replace_exts": sorted(jobs_mod.REPLACE_EXTS)})

    # -- weight files (Settings -> Upscale) -------------------------------
    def api_weights():
        out = []
        for p in custom_weights():
            st = os.stat(p)
            out.append({"name": os.path.basename(p), "path": p, "size": st.st_size, "mtime": st.st_mtime,
                        "selected": p == host.config.get("upscale_esrgan_weights")})
        stock = [{"type": k, "label": v[0], "file": v[2], "factor": v[3], "note": v[4],
                  "downloaded": os.path.exists(os.path.join(stock_dir(), v[2]))} for k, v in STOCK.items()]
        return jsonify({"success": True, "custom": out, "stock": stock, "folder": custom_dir(),
                        "spandrel": bool(HAVE_TORCH and upscaler.HAVE_SPANDREL)})

    def api_weights_upload():
        f = request.files.get("file")
        name = secure_filename(os.path.basename(f.filename or "")) if f else ""
        if not name or not name.lower().endswith(WEIGHT_EXTS):
            return jsonify({"success": False, "error": "a .pth, .pt, .ckpt or .safetensors file is needed"}), 400
        dest = os.path.join(custom_dir(), name)
        if os.path.exists(dest):
            return jsonify({"success": False, "error": "%s already exists" % name}), 409
        tmp = dest + ".part"
        f.save(tmp)
        try:
            # the suffix decides the reader: check under the final name's extension
            check = tmp + os.path.splitext(name)[1]
            os.replace(tmp, check)
            up = upscaler.load_model(check, "cpu")
            info = {"arch": up.arch, "scale": up.scale}
            del up
            os.replace(check, dest)
        except Exception as e:
            for p in (tmp, tmp + os.path.splitext(name)[1]):
                if os.path.exists(p):
                    os.remove(p)
            return jsonify({"success": False, "error": "not a usable upscaling model: %s" % e}), 400
        core.audit("upscale_weights_add", f"file={name!r}")
        return jsonify({"success": True, "name": name, "path": dest, **info})

    def api_weights_delete():
        name = os.path.basename(str((request.get_json(silent=True) or {}).get("name") or ""))
        p = os.path.join(custom_dir(), name)
        if not name or not os.path.isfile(p):
            return jsonify({"success": False, "error": "no such file"}), 404
        if host.config.get("upscale_esrgan_weights") == p:
            host.set_config("upscale_esrgan_weights", "")
        model_registry.clear(f"esrgan:{p}:")
        os.remove(p)
        core.audit("upscale_weights_delete", f"file={name!r}")
        return jsonify({"success": True})

    host.add_route("/api/upscale", api_run, methods=["POST"], feature=FEATURE, level="write",
                   action="upscale", fields=("filename", "filenames", "mode", "factor"))
    host.add_route("/api/upscale/jobs", api_jobs, feature=FEATURE)
    host.add_route("/api/upscale/cancel", api_cancel, methods=["POST"], feature=FEATURE, level="write")
    host.add_route("/api/upscale/info", api_info, feature=FEATURE)
    host.add_route("/api/upscale/weights", api_weights, feature="settings." + TAB)
    host.add_route("/api/upscale/weights/upload", api_weights_upload, methods=["POST"],
                   feature="settings." + TAB, level="write", admin=True)
    host.add_route("/api/upscale/weights/delete", api_weights_delete, methods=["POST"],
                   feature="settings." + TAB, level="write", admin=True)

    host.add_asset("upscale.js")
    host.add_asset("upscale.css", kind="css")
    host.provide_service("upscale", {"submit": jobs.submit, "job": jobs.get, "jobs": jobs.list,
                                     "cancel": jobs.cancel, "run_job": jobs.run})
    log.info("upscale module registered")