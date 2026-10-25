"""! @file
@brief OpenAI-compatible vision chat client (the vlm module's "llm" service).
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


class _Gate:
    """! @brief In-flight cap for one server (+ model). Every request to it, from any
    thread (background sweep, a bulk run's pool, a pipeline fanning out over
    subjects), waits here, so the server gets at most `limit` requests at once
    and they all stay in flight together: that is what lets a server with
    parallel slots batch them. An interactive call gets one extra slot and goes
    before waiting background calls, so a click never queues behind a sweep."""

    def __init__(self):
        self.cv = threading.Condition()
        self.inflight = 0
        self.fg_waiting = 0

    @contextlib.contextmanager
    def hold(self, limit, foreground):
        limit = max(1, int(limit or 1))
        with self.cv:
            if foreground:
                self.fg_waiting += 1
                try:
                    while self.inflight >= limit + 1:
                        self.cv.wait()
                finally:
                    self.fg_waiting -= 1
            else:
                while self.inflight >= limit or self.fg_waiting:
                    self.cv.wait()
            self.inflight += 1
        try:
            yield
        finally:
            with self.cv:
                self.inflight -= 1
                self.cv.notify_all()


_GATES = {}
_GATES_LOCK = threading.Lock()


def _gate(key):
    with _GATES_LOCK:
        g = _GATES.get(key)
        if g is None:
            g = _GATES[key] = _Gate()
        return g


def _post(url, *, json, headers, timeout, gate=None, limit=None):
    """! @brief POST to the endpoint. An interactive call (not on a worker thread)
    marks the resource busy so the sweep hands out no new background slot
    while it waits; a busy/overloaded reply is retried with backoff instead
    of failing the job. `gate` (default: the URL's server) caps the requests in
    flight on it at `limit` (default: oai_concurrency)."""
    tm = getattr(HOST, "thread_manager", None)
    foreground = tm is not None and not tm.in_worker()
    fg = tm.foreground_use(RESOURCE) if foreground else contextlib.nullcontext()
    if limit is None:
        try:
            limit = int(_cfg().get("oai_concurrency") or 1)
        except (TypeError, ValueError, AttributeError):
            limit = 1
    with fg, _gate(gate or v1_base(url)).hold(limit, foreground):
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
    """! @brief Clamp a normalised center-form box to the image bounds; None if malformed."""
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
    """! @brief Reduce any OpenAI-compatible URL to its `.../v1` base (no trailing
    slash), stripping a known operation suffix if present. '' -> ''."""
    base = (endpoint or "").strip().rstrip('/')
    if not base:
        return ""
    for suffix in ("/chat/completions", "/completions", "/embeddings"):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base

def chat_url(endpoint):
    """! @brief Auto-complete a base URL to the OpenAI chat-completions path."""
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
    """! @brief Model ids the endpoint reports on GET /v1/models (OpenAI, koboldcpp,
    llama.cpp, vLLM, Ollama...). [] when the server has no list (older backends)
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


def _int(v, default=1):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _tier(tier="main"):
    """! @brief (endpoint, model, key, concurrency) of a tier. "main" is the model that
    answers first; "verify" the one that confirms (oai_verify_*). The verify tier
    reuses the main endpoint when its own is blank (another model on the same
    server), and the main key only then: a key is never sent to another host."""
    c = _cfg()
    if tier == "verify":
        own = (c.get("oai_verify_endpoint") or "").strip()
        endpoint = own or c.get("oai_endpoint", "")
        key = (c.get("oai_verify_key") or "").strip() or ("" if own else (c.get("oai_key") or "").strip())
        return (endpoint, (c.get("oai_verify_model") or "").strip(), key,
                max(1, _int(c.get("oai_verify_concurrency"), 1)))
    return (c.get("oai_endpoint", ""), (c.get("oai_model") or "").strip(), (c.get("oai_key") or "").strip(),
            max(1, _int(c.get("oai_concurrency"), 1)))


def verify_configured():
    """! @brief True when a confirming model is set (oai_verify_model)."""
    endpoint, model, _k, _c = _tier("verify")
    return bool(chat_url(endpoint) and model)


def request(messages, tools=None, tool_choice=None, timeout=600, endpoint=None, *,
            tier="main", max_tokens=1000, extra=None, full=False):
    """! @brief Low-level OpenAI-compatible chat call. Returns the message dict or raises.
    `endpoint` overrides the configured one (used to spread load across several
    model instances during a parallel pipeline run). `tier` picks the answering
    ("main") or the confirming ("verify") model. `extra` is merged into the
    payload; `full=True` returns the whole choice (message, finish_reason,
    logprobs) instead of the message."""
    t_endpoint, model, key, limit = _tier(tier)
    endpoint = chat_url(endpoint or t_endpoint)
    if not endpoint or not model:
        raise RuntimeError("LLM not configured" if tier == "main" else "verify model not configured")
    hdrs = {"Content-Type": "application/json"}
    if key:
        hdrs["Authorization"] = f"Bearer {key}"
    payload = {"model": model, "max_tokens": max_tokens, "messages": messages}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice
    if extra:
        payload.update(extra)
    r = _post(endpoint, headers=hdrs, json=payload, timeout=timeout,
              gate=v1_base(endpoint) + "|" + model, limit=limit)
    choice = r.json()["choices"][0]
    return choice if full else choice["message"]

BOX_TOOL = [{"type": "function", "function": {
    "name": "create_bounding_boxes",
    "description": "Bounding boxes normalised 0..1",
    "parameters": {"type": "object", "properties": {"boxes": {"type": "array", "items": {
        "type": "object", "properties": {
            "class_name": {"type": "string"}, "cx": {"type": "number"},
            "cy": {"type": "number"}, "w": {"type": "number"}, "h": {"type": "number"}},
        "required": ["class_name", "cx", "cy", "w", "h"]}}}, "required": ["boxes"]}}}]

def encode_image(image_bgr, quality=85):
    """! @brief JPEG-encode a BGR image to a data-URL: the single chokepoint for every
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

def _messages(prompt, url):
    """! @brief System prompt, then the IMAGE, then the question. Everything a server
    can cache comes first: calls about the same image (the per-subject steps,
    the overall steps, the confirming call) differ only after the image, so a
    server with prompt caching (llama.cpp, koboldcpp, vLLM, the paid APIs)
    reuses the encoded image instead of processing it again per question."""
    content = []
    if url:
        content.append({"type": "image_url", "image_url": {"url": url}})
    content.append({"type": "text", "text": prompt})
    return [{"role": "system", "content": _cfg().get("oai_system_prompt", "")},
            {"role": "user", "content": content}]


_BOX_CHOICE = {"type": "function", "function": {"name": "create_bounding_boxes"}}
VERIFY_MODES = ("off", "flagged", "always")
_SHORT = ("bool", "choice")         # answers a token's probability says something about


def _boxes_of(msg):
    """! @brief (raw boxes, parse_failed) from a tool call or a JSON reply."""
    boxes, failed = [], False
    if msg.get("tool_calls"):
        try:
            boxes = json.loads(msg["tool_calls"][0]["function"]["arguments"]).get("boxes", [])
        except Exception:
            failed = True
    if not boxes and msg.get("content"):
        c = msg["content"]
        if "{" in c:
            try:
                boxes = json.loads(c[c.find('{'):c.rfind('}') + 1]).get("boxes", [])
                failed = False
            except Exception:
                failed = True
    return (boxes if isinstance(boxes, list) else []), failed


def parse(want, msg, choices=None):
    """! @brief A reply -> (value, flags). The value is what call() always returned
    for `want`; flags name what looks wrong with it (empty, truncated, ambiguous,
    malformed), which is what the confirming model is asked about in "flagged"
    mode. No flags = the answer is well-formed (not necessarily right)."""
    flags = []
    if want == "boxes":
        raw, failed = _boxes_of(msg)
        boxes = [cb for cb in (clamp_box(b) for b in raw if isinstance(b, dict)) if cb]
        if failed:
            flags.append("malformed")
        elif len(boxes) != len(raw):
            flags.append("bad boxes")
        return boxes, flags
    text = (msg.get("content") or "").strip()
    if not text:
        flags.append("empty")
    if want == "tags":
        tags = [t.strip() for t in re.split(r'[,\n]', text) if t.strip()]
        if text and not tags:
            flags.append("empty")
        if any(len(t.split()) > 6 for t in tags):
            flags.append("sentence in tags")
        return tags, flags
    if want == "bool":
        low = text.lower().lstrip("*_ \"'`")
        val = low.startswith(("y", "true")) or low[:8].find("yes") != -1
        if text and not low.startswith(("yes", "no", "true", "false")):
            flags.append("unclear yes/no")
        return val, flags
    if want == "choice":
        low = text.lower()
        if choices:
            hit = [c for c in choices if c.lower() in low]
            if text and len(hit) != 1:
                flags.append("no choice" if not hit else "several choices")
            return (hit[0] if hit else choices[0]), flags
        return text, flags
    if want == "json":
        try:
            val = json.loads(text[text.find('{'):text.rfind('}') + 1])
        except Exception:
            val = None
        if not isinstance(val, dict):
            if text:
                flags.append("malformed")
            return {}, flags
        if not val and "empty" not in flags:
            flags.append("empty")
        return val, flags
    if want == "name" and (len(text.split()) > 8 or "\n" in text):
        flags.append("not a name")
    return text, flags


def _first_token_prob(choice):
    """! @brief Probability of the reply's first token, when the server sent logprobs."""
    try:
        lp = (choice.get("logprobs") or {}).get("content") or []
        return float(np.exp(lp[0]["logprob"])) if lp else None
    except Exception:
        return None


def _draft_text(want, value):
    if want == "boxes":
        return json.dumps({"boxes": value})
    if want == "json":
        return json.dumps(value, ensure_ascii=False)
    if want == "tags":
        return ", ".join(value or [])
    if want == "bool":
        return "yes" if value else "no"
    return str(value or "")


_VERIFY_FORMAT = {
    "boxes": 'JSON: {"boxes": [{"class_name": "...", "cx": 0..1, "cy": 0..1, "w": 0..1, "h": 0..1}]}',
    "json": "JSON, the same keys the task asks for",
    "tags": "a comma-separated tag list",
    "bool": "yes or no",
    "choice": "one of the allowed choices",
    "name": "only the name",
}

VERIFY_PROMPT = ("{prompt}\n\n---\nA smaller model answered this:\n{draft}\n{flags}"
                 "Check that answer against the image and the task. If it is correct and complete, "
                 "reply with exactly OK. Otherwise reply with only the corrected answer, as {fmt}.")


def _is_ok(text):
    t = (text or "").strip().strip(".!*_\"'` ").upper()
    return t == "OK" or t == "OKAY"


def verify(prompt, url, want, choices, draft, flags=None, timeout=600):
    """! @brief Ask the confirming model about a draft answer. It replies OK (a few
    tokens: the draft stands) or the corrected answer, parsed like the draft.
    Any failure keeps the draft, so a down verify server never fails a run."""
    fmt = _VERIFY_FORMAT.get(want, "plain text")
    if want == "choice" and choices:
        fmt = "one of: " + ", ".join(choices)
    vp = VERIFY_PROMPT.format(prompt=prompt, draft=_draft_text(want, draft),
                              flags=("It looked wrong because: %s.\n" % ", ".join(flags)) if flags else "",
                              fmt=fmt)
    try:
        msg = request(_messages(vp, url), timeout=timeout, tier="verify")
    except Exception as e:
        _log("verify call failed, draft kept: %s" % e)
        return draft, False
    if _is_ok(msg.get("content")):
        return draft, False
    fixed, fixed_flags = parse(want, msg, choices)
    if fixed_flags:
        return draft, False          # the correction is no better formed than the draft
    return fixed, True


def _log(msg):
    lg = getattr(HOST, "logger", None)
    if lg is not None:
        try:
            lg.warning(msg)
        except Exception:
            pass


STATS = {"calls": 0, "flagged": 0, "verified": 0, "corrected": 0}
_STATS_LOCK = threading.Lock()


def _count(**kw):
    with _STATS_LOCK:
        for k, v in kw.items():
            STATS[k] = STATS.get(k, 0) + v


def call(prompt, image_bgr, want="text", choices=None, endpoint=None, verify_mode=None):
    """! @brief Typed single-turn call used by the pipeline engine. `want` controls parsing.
    `endpoint` (optional) pins this call to a specific model instance.

    Two tiers: the main model answers; with a confirming model configured
    (oai_verify_model) and oai_verify_mode (or `verify_mode` for this call)
    "always", or "flagged" and the answer is flagged (see parse(); with
    oai_verify_min_prob > 0 also a yes/no or choice answered with a first-token
    probability under it), the confirming model checks the answer and replies
    OK or a correction. The image is encoded once and sent first both times."""
    url = encode_image(image_bgr) if image_bgr is not None else None
    messages = _messages(prompt, url)
    cfg = _cfg()
    mode = verify_mode if verify_mode in VERIFY_MODES else cfg.get("oai_verify_mode", "off")
    if mode not in VERIFY_MODES:
        mode = "off"
    if mode != "off" and not verify_configured():
        mode = "off"
    try:
        min_prob = float(cfg.get("oai_verify_min_prob") or 0)
    except (TypeError, ValueError):
        min_prob = 0.0
    extra = {"logprobs": True} if (mode == "flagged" and min_prob > 0 and want in _SHORT) else None

    if want == "boxes":
        choice = request(messages, BOX_TOOL, _BOX_CHOICE, endpoint=endpoint, full=True)
    else:
        choice = request(messages, endpoint=endpoint, extra=extra, full=True)
    msg = choice.get("message") or {}
    value, flags = parse(want, msg, choices)
    if choice.get("finish_reason") == "length":
        flags.append("cut off")
    if extra:
        p = _first_token_prob(choice)
        if p is not None and p < min_prob:
            flags.append("unsure (p=%.2f)" % p)
    _count(calls=1, flagged=1 if flags else 0)
    if mode == "always" or (mode == "flagged" and flags):
        value, changed = verify(prompt, url, want, choices, value, flags)
        _count(verified=1, corrected=1 if changed else 0)
    return value


# -- embeddings (OpenAI-compatible /v1/embeddings) ----------------------------
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
    """! @brief Which space the vectors live in - stored with each embedding row."""
    return "oai:" + embed_model()


def _normalise(v):
    v = np.asarray(v, np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def embed_request(inputs, timeout=120, model=None):
    endpoint, model = embed_url(), (model or embed_model())
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
    """! @brief Image -> L2-normalised vector via the embeddings endpoint (multimodal
    embedding models such as CLIP-style servers accept data-URL inputs)."""
    url = encode_image(img_bgr)
    if not url:
        return None
    vecs = embed_request([url], timeout=timeout)
    return _normalise(vecs[0]) if vecs else None


def embed_text(text, timeout=60, model=None):
    """! @brief Text -> vector in the same space as embed_image (what semantic search
    needs); None on failure. `model` overrides the configured embedding model
    (the text-only pick for passages)."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        vecs = embed_request([text], timeout=timeout, model=model)
        return _normalise(vecs[0]) if vecs else None
    except Exception:
        return None