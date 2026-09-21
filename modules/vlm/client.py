"""
OpenAI-compatible vision chat client (the vlm module's "llm" service).
======================================================================
The core keeps thin wrappers (_llm_call / _llm_request / _encode_for_llm)
that delegate here, so the pipeline, AI actions, comics and books all talk
to one client that this module owns and configures.
"""
import base64
import contextlib
import json
import random
import re
import threading
import time

import numpy as np
import requests

from optional_deps import optional_import
cv2, _HAVE_CV2 = optional_import("cv2")

HOST = None      # bound by module.register

# The endpoint is one shared resource however many capabilities point at it.
# Providers register with this name so the background sweep runs at most
# `oai_concurrency` requests on it at a time (see thread_manager slots).
RESOURCE = "oai-endpoint"
_BUSY = (429, 502, 503, 504)


def _post(url, *, json, headers, timeout):
    """POST to the endpoint. An interactive call (not on a worker thread)
    marks the resource busy so the sweep hands out no new background slot
    while it waits; a busy/overloaded reply is retried with backoff instead
    of failing the job."""
    tm = getattr(HOST, "thread_manager", None)
    fg = (tm.foreground_use(RESOURCE) if tm is not None and not tm.in_worker()
          else contextlib.nullcontext())
    with fg:
        for attempt in range(4):
            r = requests.post(url, json=json, headers=headers, timeout=timeout)
            if r.status_code in _BUSY and attempt < 3:
                time.sleep(2 * 2 ** attempt + random.random())      # 2s, 4s, 8s (+jitter)
                continue
            r.raise_for_status()
            return r


def _cfg():
    return HOST.config


def clamp_box(b: dict):
    """Clamp a normalised center-form box to the image bounds; None if malformed."""
    try:
        cx, cy, w, h = float(b["cx"]), float(b["cy"]), float(b["w"]), float(b["h"])
    except (KeyError, TypeError, ValueError):
        return None
    x1, y1 = max(0.0, cx - w / 2), max(0.0, cy - h / 2)
    x2, y2 = min(1.0, cx + w / 2), min(1.0, cy + h / 2)
    if x2 - x1 < 1e-4 or y2 - y1 < 1e-4:
        return None
    nb = dict(b)
    nb["cx"], nb["cy"] = (x1 + x2) / 2, (y1 + y2) / 2
    nb["w"], nb["h"] = x2 - x1, y2 - y1
    return nb


def v1_base(endpoint):
    """Reduce any OpenAI-compatible URL to its `.../v1` base (no trailing
    slash), stripping a known operation suffix if present. '' -> ''."""
    base = (endpoint or "").strip().rstrip('/')
    if not base:
        return ""
    for suffix in ("/chat/completions", "/completions", "/embeddings"):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base

def chat_url(endpoint):
    """Auto-complete a base URL to the OpenAI chat-completions path."""
    base = v1_base(endpoint)
    if not base:
        return ""
    return base + ("/chat/completions" if base.endswith("/v1")
                   else "/v1/chat/completions")

_MODELS_CACHE = {}     # v1 base -> (expires, [ids])
_MODELS_INFLIGHT = set()

def _fetch_models(base, key, ttl):
    hdrs = {"Authorization": f"Bearer {key}"} if key else {}
    ids = []
    try:
        r = requests.get(base + ("" if base.endswith("/v1") else "/v1") + "/models",
                         headers=hdrs, timeout=3)
        r.raise_for_status()
        d = r.json()
        rows = d.get("data") if isinstance(d, dict) else d
        ids = [str(m.get("id") or m.get("name")) for m in (rows or []) if isinstance(m, dict)]
        ids = [i for i in ids if i and i != "None"]
    except Exception:
        ids = []
    _MODELS_CACHE[base] = (time.time() + ttl, ids)
    _MODELS_INFLIGHT.discard(base)
    return ids

def list_models(endpoint=None, key=None, ttl=20, wait=False):
    """Model ids the endpoint reports on GET /v1/models (OpenAI, koboldcpp,
    llama.cpp, vLLM, Ollama…). [] when the server has no list (older backends)
    or is down, so the settings field falls back to manual entry.

    Never blocks the caller on the network unless wait=True: a stale/missing
    entry is refreshed on a background thread and the last known list (or [])
    is returned now, so opening Settings stays instant while a model server is
    still coming up."""
    base = v1_base(endpoint or _cfg().get("oai_endpoint", ""))
    if not base:
        return []
    key = (key if key is not None else _cfg().get("oai_key", "")).strip()
    hit = _MODELS_CACHE.get(base)
    if hit and hit[0] > time.time():
        return hit[1]
    if wait:
        return _fetch_models(base, key, ttl)
    if base not in _MODELS_INFLIGHT:
        _MODELS_INFLIGHT.add(base)
        threading.Thread(target=_fetch_models, args=(base, key, ttl), daemon=True).start()
    return hit[1] if hit else []


def request(messages, tools=None, tool_choice=None, timeout=600, endpoint=None):
    """Low-level OpenAI-compatible chat call. Returns the message dict or raises.
    `endpoint` overrides the configured one (used to spread load across several
    model instances during a parallel pipeline run)."""
    endpoint = chat_url(endpoint or _cfg().get("oai_endpoint", ""))
    model    = _cfg().get("oai_model", "").strip()
    key      = _cfg().get("oai_key", "").strip()
    if not endpoint or not model:
        raise RuntimeError("LLM not configured")
    hdrs = {"Content-Type": "application/json"}
    if key:
        hdrs["Authorization"] = f"Bearer {key}"
    payload = {"model": model, "max_tokens": 1000, "messages": messages}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice
    r = _post(endpoint, headers=hdrs, json=payload, timeout=timeout)
    return r.json()["choices"][0]["message"]

BOX_TOOL = [{"type": "function", "function": {
    "name": "create_bounding_boxes",
    "description": "Bounding boxes normalised 0..1",
    "parameters": {"type": "object", "properties": {"boxes": {"type": "array", "items": {
        "type": "object", "properties": {
            "class_name": {"type": "string"}, "cx": {"type": "number"},
            "cy": {"type": "number"}, "w": {"type": "number"}, "h": {"type": "number"}},
        "required": ["class_name", "cx", "cy", "w", "h"]}}}, "required": ["boxes"]}}}]

def encode_image(image_bgr, quality=85):
    """JPEG-encode a BGR image to a data-URL: the single chokepoint for every
    image sent to the vision LLM (pipeline, prompted detection, AI actions).
    Modules subscribed to `llm.image` (llm_preprocess: compress/pad) transform
    it first. Returns the data-URL string, or None if encoding fails."""
    if image_bgr is None:
        return None
    for out in HOST.emit("llm.image", image=image_bgr):
        if out is not None:
            image_bgr = out
    ok, buf = cv2.imencode('.jpg', image_bgr, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        return None
    b64 = base64.b64encode(buf.tobytes()).decode()
    return f"data:image/jpeg;base64,{b64}"

def call(prompt, image_bgr, want="text", choices=None, endpoint=None):
    """Typed single-turn call used by the pipeline engine. `want` controls parsing.
    `endpoint` (optional) pins this call to a specific model instance."""
    content = [{"type": "text", "text": prompt}]
    if image_bgr is not None:
        url = encode_image(image_bgr)
        if url:
            content.append({"type": "image_url",
                            "image_url": {"url": url}})
    messages = [{"role": "system", "content": _cfg().get("oai_system_prompt", "")},
                {"role": "user", "content": content}]

    if want == "boxes":
        msg = request(messages, BOX_TOOL,
                           {"type": "function", "function": {"name": "create_bounding_boxes"}},
                           endpoint=endpoint)
        boxes = []
        if msg.get("tool_calls"):
            try:
                boxes = json.loads(msg["tool_calls"][0]["function"]["arguments"]).get("boxes", [])
            except Exception:
                pass
        if not boxes and msg.get("content"):
            try:
                c = msg["content"]; boxes = json.loads(c[c.find('{'):c.rfind('}')+1]).get("boxes", [])
            except Exception:
                pass
        return [cb for cb in (clamp_box(b) for b in boxes) if cb]

    msg  = request(messages, endpoint=endpoint)
    text = (msg.get("content") or "").strip()
    if want == "tags":
        return [t.strip() for t in re.split(r'[,\n]', text) if t.strip()]
    if want == "bool":
        low = text.lower().lstrip("*_ \"'`")
        return low.startswith(("y", "true")) or low[:8].find("yes") != -1
    if want == "choice":
        low = text.lower()
        if choices:
            for c in choices:
                if c.lower() in low:
                    return c
            return choices[0]
        return text
    if want == "json":
        try:
            return json.loads(text[text.find('{'):text.rfind('}')+1])
        except Exception:
            return {}
    return text


# ── embeddings (OpenAI-compatible /v1/embeddings) ────────────────────────────
def embed_url(endpoint=None):
    base = v1_base(endpoint or _cfg().get("oai_endpoint", ""))
    if not base:
        return ""
    return base + ("/embeddings" if base.endswith("/v1") else "/v1/embeddings")


def embed_model():
    return (_cfg().get("oai_embed_model") or "").strip()


def embed_configured():
    return bool(embed_model()) and bool(embed_url())


def embed_tag():
    """Which space the vectors live in — stored with each embedding row."""
    return "oai:" + embed_model()


def _normalise(v):
    v = np.asarray(v, np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def embed_request(inputs, timeout=120):
    endpoint, model = embed_url(), embed_model()
    if not endpoint or not model:
        raise RuntimeError("OAI embeddings not configured")
    key = (_cfg().get("oai_key") or "").strip()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    r = _post(endpoint, json={"model": model, "input": inputs}, headers=headers,
              timeout=timeout)
    data = sorted(r.json().get("data", []), key=lambda d: d.get("index", 0))
    return [np.asarray(d["embedding"], np.float32) for d in data]


def embed_image(img_bgr, timeout=120):
    """Image -> L2-normalised vector via the embeddings endpoint (multimodal
    embedding models such as CLIP-style servers accept data-URL inputs)."""
    url = encode_image(img_bgr)
    if not url:
        return None
    vecs = embed_request([url], timeout=timeout)
    return _normalise(vecs[0]) if vecs else None


def embed_text(text, timeout=60):
    """Text -> vector in the same space as embed_image (what semantic search
    needs); None on failure."""
    try:
        vecs = embed_request([text], timeout=timeout)
        return _normalise(vecs[0]) if vecs else None
    except Exception:
        return None