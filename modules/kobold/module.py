"""
KoboldCpp module.
======================================================================
Runs a local koboldcpp (github.com/LostRuins/koboldcpp) server from inside
the app and points the vlm module at it. The model field takes:

  a local .gguf path
  an https URL to a .gguf (koboldcpp downloads it itself)
  a HuggingFace path  owner/repo/file.gguf  -> rewritten to the HF resolve URL

so koboldcpp's own downloader fetches the weights on first start (into
models/kobold/, the process's cwd). The same goes for --mmproj, which is
what makes a text model vision-capable for the VLM providers.

Once the server answers, the module writes oai_endpoint / oai_model into
the vlm settings (from koboldcpp's own /v1/models list), so every VLM
capability runs against it with no further setup.

No koboldcpp installed? The tab downloads the official Linux build for the
selected GPU flavour (koboldai.org short links) into models/kobold/bin/ and
points the executable setting at it.

Routes (Settings → Kobold tab, kobold.js): /api/kobold/status, /start,
/stop, /download.
"""

import atexit
import collections
import os
import shlex
import subprocess
import sys
import threading
import time

import requests
from flask import jsonify

import model_registry

MANIFEST = {
    "id":          "kobold",
    "name":        "KoboldCpp",
    "version":     "1.0.0",
    "description": "Run a local koboldcpp server (GGUF, HuggingFace auto-download) and "
                   "feed it to the vision-LLM module as its endpoint.",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "assets":      ["kobold.js"],
}

_PROC = None
_LOG = collections.deque(maxlen=300)
_LOCK = threading.Lock()
_DL = {"busy": False}

# Official builds (koboldai.org short links, always the latest release).
_BUILDS = {
    "cu12":  ("https://koboldai.org/cpplinuxcu12", "Linux · Nvidia (CUDA 12)"),
    "cu11":  ("https://koboldai.org/cpplinux",     "Linux · Nvidia legacy (CUDA 9-11)"),
    "nocu":  ("https://koboldai.org/cpplinuxnocu", "Linux · no Nvidia (Vulkan/CPU)"),
    "rocm":  ("https://koboldai.org/cpplinuxrocm", "Linux · AMD (ROCm)"),
}
RELEASES_URL = "https://koboldai.org/cpp"


def _bin_dir():
    return model_registry.model_dir("kobold", "bin")


def download(variant, cfg, save):
    """Fetch the chosen build to models/kobold/bin/koboldcpp-<variant>, make it
    executable and set it as the executable. Runs on a thread; logs progress."""
    url, label = _BUILDS[variant]
    dest = os.path.join(_bin_dir(), f"koboldcpp-{variant}")
    tmp = dest + ".part"
    _DL["busy"] = True
    try:
        _LOG.append(f"[download] {label}: {url}")
        with requests.get(url, stream=True, timeout=60, allow_redirects=True) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            done, last = 0, 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk); done += len(chunk)
                    if total and done - last > total / 20:
                        last = done
                        _LOG.append(f"[download] {done * 100 // total}% ({done >> 20} MB)")
        if os.path.getsize(tmp) < 10 << 20:
            raise RuntimeError("download too small to be koboldcpp (bad link?)")
        os.replace(tmp, dest)
        os.chmod(dest, 0o755)
        cfg["kobold_exe"] = dest
        save()
        _LOG.append(f"[download] done → {dest} (set as executable)")
    except Exception as e:
        _LOG.append(f"[download] failed: {e}")
        if os.path.exists(tmp):
            os.remove(tmp)
    finally:
        _DL["busy"] = False


def _hf(path: str) -> str:
    """owner/repo/file.gguf -> https://huggingface.co/owner/repo/resolve/main/file.gguf;
    URLs and local paths pass through."""
    p = (path or "").strip()
    if not p or p.startswith(("http://", "https://")) or os.path.exists(p):
        return p
    parts = p.split("/")
    if len(parts) >= 3 and parts[-1].lower().endswith(".gguf"):
        return f"https://huggingface.co/{parts[0]}/{parts[1]}/resolve/main/{'/'.join(parts[2:])}"
    return p


def _pump(proc):
    for line in iter(proc.stdout.readline, b""):
        _LOG.append(line.decode("utf-8", "replace").rstrip())
    _LOG.append(f"[koboldcpp exited {proc.poll()}]")


def _alive():
    return _PROC is not None and _PROC.poll() is None


def _url(cfg, path=""):
    return f"http://127.0.0.1:{int(cfg.get('kobold_port') or 5001)}{path}"


def _ready(cfg):
    try:
        return requests.get(_url(cfg, "/api/extra/version"), timeout=1).ok
    except Exception:
        return False


def stop():
    global _PROC
    with _LOCK:
        if _alive():
            _PROC.terminate()
            try:
                _PROC.wait(10)
            except subprocess.TimeoutExpired:
                _PROC.kill()
        _PROC = None


atexit.register(stop)


def register(host):
    cfg = host.config
    keys = [
        ("kobold_exe", "koboldcpp", "text", "koboldcpp executable",
         "Binary on PATH, a full path, or koboldcpp.py (run with this Python)."),
        ("kobold_model", "", "text", "Model (GGUF)",
         "Local .gguf, https URL, or HuggingFace path owner/repo/file.gguf (auto-downloaded)."),
        ("kobold_mmproj", "", "text", "Vision projector (mmproj)",
         "Same forms as the model. Needed for image input (VLM)."),
        ("kobold_port", 5001, "number", "Port", ""),
        ("kobold_gpulayers", -1, "number", "GPU layers", "-1 = auto."),
        ("kobold_context", 8192, "number", "Context size", ""),
        ("kobold_backend", "auto", "select", "Backend", ""),
        ("kobold_extra_args", "", "text", "Extra arguments", "Passed verbatim, e.g. --flashattention --quantkv 1"),
        ("kobold_apply", True, "toggle", "Point the VLM module at it",
         "On start, set the vlm endpoint + chat model to this server."),
        ("kobold_autostart", False, "toggle", "Start with the app", ""),
    ]
    backends = [{"value": v, "label": l} for v, l in (
        ("auto", "Auto (koboldcpp decides)"), ("cublas", "CUDA (--usecublas)"),
        ("vulkan", "Vulkan (--usevulkan)"), ("clblast", "CLBlast (--useclblast 0 0)"),
        ("cpu", "CPU only (--usecpu)"))]
    for key, default, kind, label, help_ in keys:
        host.add_config_key(key, default=default)
        host.add_settings_field(key=key, label=label, kind=kind, pane="module", help=help_ or None,
                                options=backends if kind == "select" else None)

    def _cmd():
        exe = str(cfg.get("kobold_exe") or "koboldcpp").strip()
        cmd = [sys.executable, exe] if exe.endswith(".py") else [exe]
        model = _hf(cfg.get("kobold_model"))
        if not model:
            raise RuntimeError("no model set")
        cmd += ["--model", model, "--port", str(int(cfg.get("kobold_port") or 5001)),
                "--contextsize", str(int(cfg.get("kobold_context") or 8192)),
                "--gpulayers", str(int(cfg.get("kobold_gpulayers") if cfg.get("kobold_gpulayers") is not None else -1)),
                "--skiplauncher", "--quiet"]
        if _hf(cfg.get("kobold_mmproj")):
            cmd += ["--mmproj", _hf(cfg.get("kobold_mmproj"))]
        cmd += {"cublas": ["--usecublas"], "vulkan": ["--usevulkan"],
                "clblast": ["--useclblast", "0", "0"], "cpu": ["--usecpu"]}.get(cfg.get("kobold_backend"), [])
        cmd += shlex.split(str(cfg.get("kobold_extra_args") or ""))
        return cmd

    def _apply():
        """Write the vlm settings from koboldcpp's own model list."""
        llm = host.get_service("llm") or {}
        cfg["oai_endpoint"] = _url(cfg, "/v1/chat/completions")
        ids = llm["list_models"](cfg["oai_endpoint"], "", ttl=0) if llm.get("list_models") else []
        if ids:
            cfg["oai_model"] = ids[0]
        elif not cfg.get("oai_model"):
            cfg["oai_model"] = "koboldcpp"     # old backends: kobold ignores the name anyway
        host.save_config()

    def start():
        global _PROC
        with _LOCK:
            if _alive():
                return "already running"
            cmd = _cmd()
            _LOG.clear()
            _LOG.append("$ " + " ".join(shlex.quote(c) for c in cmd))
            _PROC = subprocess.Popen(cmd, cwd=model_registry.model_dir("kobold", "gguf"),
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            threading.Thread(target=_pump, args=(_PROC,), daemon=True).start()

        def _wait():   # downloads can take a while; apply the endpoint once it answers
            for _ in range(3600):
                if not _alive():
                    return
                if _ready(cfg):
                    if cfg.get("kobold_apply"):
                        _apply()
                    _LOG.append("[ready]")
                    return
                time.sleep(1)
        threading.Thread(target=_wait, daemon=True).start()
        return "starting"

    def _status():
        return {"running": _alive(), "ready": _alive() and _ready(cfg),
                "downloading": _DL["busy"], "exe": cfg.get("kobold_exe") or "",
                "builds": [{"value": k, "label": v[1]} for k, v in _BUILDS.items()],
                "releases_url": RELEASES_URL,
                "pid": _PROC.pid if _alive() else None, "url": _url(cfg),
                "endpoint_applied": (cfg.get("oai_endpoint") or "").startswith(_url(cfg)),
                "log": list(_LOG)[-80:]}

    def api_status():
        return jsonify({"success": True, **_status()})

    def api_start():
        try:
            note = start()
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})
        return jsonify({"success": True, "note": note, **_status()})

    def api_stop():
        stop()
        return jsonify({"success": True, **_status()})

    def api_download():
        from flask import request
        variant = (request.json or {}).get("variant") or "cu12"
        if variant not in _BUILDS:
            return jsonify({"success": False, "error": "unknown build"})
        if _DL["busy"]:
            return jsonify({"success": False, "error": "download already running"})
        threading.Thread(target=download, args=(variant, cfg, host.save_config), daemon=True).start()
        return jsonify({"success": True, **_status()})

    host.add_route("/api/kobold/download", api_download, methods=["POST"], feature="settings",
                   level="write", action="kobold_download", fields=("variant",))
    host.add_route("/api/kobold/status", api_status, feature="settings")
    host.add_route("/api/kobold/start", api_start, methods=["POST"], feature="settings",
                   level="write", action="kobold_start")
    host.add_route("/api/kobold/stop", api_stop, methods=["POST"], feature="settings",
                   level="write", action="kobold_stop")
    host.add_settings_tab("kobold", "Kobold", icon="🐲", admin_only=True)
    host.add_asset("kobold.js")
    host.provide_service("kobold", {"start": start, "stop": stop, "status": _status})

    if cfg.get("kobold_autostart") and cfg.get("kobold_model"):
        try:
            start()
        except Exception as e:
            host.logger.error(f"kobold autostart: {e}")
    host.logger.info("kobold module: registered /api/kobold/{status,start,stop}")