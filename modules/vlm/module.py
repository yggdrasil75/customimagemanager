"""
Vision-LLM model provider.
======================================================================
Registers the configured OpenAI-compatible vision chat model as a provider
for every capability a prompt can drive:

  detect     open-vocabulary boxes          (foreground-only, needs a prompt)
  classify   pick from the app's class list
  tag        free-form tags                 (until a WD-style tagger is added)
  describe   caption / description
  iqa        a 0..1 quality judgement       (rough: an opinion, not a metric)

Each provider ships a default prompt as an editable setting that shows in
the Models tab only while this provider is the pick for that capability.
A runtime prompt (an AI action's text, a detect query) overrides it.
Uses core's chat plumbing (host.core.llm_call) so this is only a seam.
"""
from flask import jsonify

from . import client, actions

MANIFEST = {
    "id":          "vlm",
    "name":        "Vision LLM (OpenAI-compatible)",
    "version":     "1.0.0",
    "description": "Uses the configured vision-capable chat model for detection, "
                   "classification, tagging, description and quality judgement.",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "assets":      ["actions.js"],
}

_PROMPTS = {
    "detect":   "Find every distinct object.",
    "classify": "Which of these labels best describes the image? Labels: {classes}. "
                "Respond ONLY as JSON: {\"class_name\": \"<label>\", \"conf\": 0..1}.",
    "tag":      "List concise lowercase tags describing the subject, setting, style, "
                "mood and notable details of this image. Respond ONLY as a JSON list of strings.",
    "describe": "Describe this image in two or three factual sentences: the subject, "
                "the setting, and anything notable. No preamble.",
    "iqa":      "Judge the technical quality of this image (sharpness, exposure, noise, "
                "compression, composition). Respond ONLY as JSON: {\"quality\": 0..1, "
                "\"reason\": \"short\"} where 1 is excellent.",
}


def register(host):
    core = host.core
    client.HOST = host

    # The OpenAI-compatible connection is this module's: endpoint, key, chat
    # model and the system prompt (the embedding module shares endpoint/key).
    host.add_config_key("oai_endpoint", default="http://localhost:5001/v1/chat/completions",
                        validate=lambda v: str(v or ""))
    host.add_config_key("oai_key", default="", validate=lambda v: str(v or ""))
    host.add_config_key("oai_model", default="gpt-4o-mini", validate=lambda v: str(v or ""))
    host.add_config_key("oai_system_prompt",
                        default="You are an expert image analysis AI. Provide concise, highly "
                                "detailed, and accurate responses.",
                        validate=lambda v: str(v or ""))
    host.add_settings_field(key="oai_endpoint", label="OpenAI-compatible endpoint", kind="text",
                            pane="module", help="Base URL or /v1/chat/completions.")
    host.add_settings_field(key="oai_key", label="API key", kind="text", pane="module")
    host.add_settings_field(key="oai_model", label="Chat model", kind="text", pane="module")
    host.add_settings_field(key="oai_system_prompt", label="System prompt", kind="textarea",
                            pane="module")
    host.add_config_key("oai_embed_model", default="", validate=lambda v: str(v or ""))
    # ── AI actions (named prompts run on an image / a selection) ─────────
    actions.HOST = host
    host.add_config_key("oai_actions", default=actions.DEFAULT_ACTIONS,
                        validate=lambda v: v if isinstance(v, list) else actions.DEFAULT_ACTIONS)
    host.register_feature("ai.llm", "LLM actions (✨ AI)", section="ai_tooling",
                          section_label="AI Tooling", default="write")
    W = core.auth.require_feature("ai.llm", level="write")
    host.add_route("/api/run_llm", W(actions.run_llm), methods=["POST"])
    host.add_route("/api/bulk_llm", W(actions.bulk_llm), methods=["POST"])
    host.add_route("/api/ai_actions", lambda: jsonify(
        {"success": True, "actions": host.config.get("oai_actions", [])}))
    host.add_asset("actions.js")
    # The actions editor is custom UI: give the module a settings tab and let
    # actions.js draw the editor into it (module-settings-tab event).
    host.add_settings_tab("vlm", "AI actions", icon="✨", admin_only=True)
    host.provide_service("ai_actions", {"apply": actions.apply, "list": lambda: host.config.get("oai_actions", [])})

    host.provide_service("llm", {"call": client.call, "request": client.request,
                                 "encode_image": client.encode_image, "v1_base": client.v1_base,
                                 "chat_url": client.chat_url, "clamp_box": client.clamp_box,
                                 "embed_image": client.embed_image, "embed_text": client.embed_text,
                                 "embed_tag": client.embed_tag,
                                 "embed_configured": client.embed_configured})

    # embed: the endpoint's /v1/embeddings (a multimodal embedding model puts
    # images and text in one space, which is what semantic text search needs).
    host.provide_model(
        "embed", "oai", label="OpenAI-compatible embeddings", family="LLM",
        speed="balanced", supports_conf=False,
        settings=[{"key": "oai_embed_model", "label": "Embedding model", "kind": "text",
                   "help": "Model name at the endpoint's /v1/embeddings, e.g. a CLIP-style "
                           "multimodal model. Text search needs one that embeds text too."}],
        loader=lambda: (lambda img, *a, **k: client.embed_image(img)),
        transform=None, available=client.embed_configured,
        reason="set the OAI endpoint and an embedding model", cost_mb=0)

    def _configured():
        return bool((host.config.get("oai_endpoint") or "").strip()
                    and (host.config.get("oai_model") or "").strip())

    def _prompt(cap, runtime=""):
        return (runtime or "").strip() or (host.config.get(f"vlm_prompt_{cap}") or "").strip() \
            or _PROMPTS[cap]

    for cap, text in _PROMPTS.items():
        host.add_config_key(f"vlm_prompt_{cap}", default=text,
                            validate=lambda v: str(v) if v is not None else "")

    def _settings(cap, help_):
        return [{"key": f"vlm_prompt_{cap}", "label": "Prompt", "kind": "textarea", "help": help_}]

    common = dict(label="Vision LLM", family="LLM", speed="accurate", supports_conf=False,
                  transform=None, available=_configured,
                  reason="set OAI endpoint + chat model in AI settings", cost_mb=0)

    # ── detect: open-vocabulary boxes (prompt required at run time) ───────
    def _detect(img_bgr, prompt="", *a, **k):
        boxes = core.llm_call(_prompt("detect", prompt) + "\n\nReturn bounding boxes normalised 0..1.",
                              core.to_bgr(img_bgr), "boxes") or []
        return [{"class_name": b.get("class_name", "object"), "cx": float(b["cx"]),
                 "cy": float(b["cy"]), "w": float(b["w"]), "h": float(b["h"])} for b in boxes]
    host.provide_model("detect", "vlm", prompted=True, loader=lambda: _detect,
                       note="Open vocabulary: finds whatever you describe. Slow and API-bound; "
                            "boxes are rough. Foreground only — it has nothing to run without a prompt.",
                       settings=_settings("detect", "Used when the caller gives no query."), **common)

    # ── classify: pick from the app's class list ───────────────────────────
    def _classify(img_bgr, prompt="", *a, **k):
        classes = [c for c in (host.config.get("classes") or []) if c and c != "object"]
        p = _prompt("classify", prompt).replace("{classes}", ", ".join(classes) or "(none)")
        res = core.llm_call(p, core.to_bgr(img_bgr), "json") or {}
        name = str(res.get("class_name", "")).strip()
        if not name:
            return []
        try:
            conf = max(0.0, min(1.0, float(res.get("conf", 0.5))))
        except (TypeError, ValueError):
            conf = 0.5
        return [{"class_name": name, "conf": conf}]
    host.provide_model("classify", "vlm", loader=lambda: _classify,
                       note="Asks the model to choose among your tag classes. {classes} in the "
                            "prompt expands to the current list.",
                       settings=_settings("classify", "{classes} expands to the app's class list."), **common)

    # ── tag: free-form tags ────────────────────────────────────────────────
    def _tag(img_bgr, prompt="", *a, **k):
        tags = core.llm_call(_prompt("tag", prompt), core.to_bgr(img_bgr), "tags") or []
        out, seen = [], set()
        for i, t in enumerate(tags):
            t = str(t).strip().lower()
            if t and t not in seen:
                seen.add(t)
                out.append({"tag": t, "conf": round(max(0.3, 1.0 - i * 0.02), 3)})
        return out
    host.provide_model("tag", "vlm", loader=lambda: _tag,
                       note="Free-form tags from the chat model. Ranked by order given; no real "
                            "confidences (a WD-style tagger gives those).",
                       settings=_settings("tag", "Ask for a JSON list of strings."), **common)

    # ── describe ───────────────────────────────────────────────────────────
    def _describe(img_bgr, prompt="", *a, **k):
        return (core.llm_call(_prompt("describe", prompt), core.to_bgr(img_bgr), "text") or "").strip()
    host.provide_model("describe", "vlm", loader=lambda: _describe,
                       note="Caption / description from the chat model.",
                       settings=_settings("describe", "The description prompt."), **common)

    # ── iqa: an opinion, not a metric ──────────────────────────────────────
    def _iqa(img_bgr, *a, **k):
        res = core.llm_call(_prompt("iqa"), core.to_bgr(img_bgr), "json") or {}
        try:
            q = max(0.0, min(1.0, float(res.get("quality", 0.5))))
        except (TypeError, ValueError):
            q = 0.5
        return {"raw": q, "quality": q, "reason": str(res.get("reason", ""))[:200]}
    host.provide_model("iqa", "vlm", loader=lambda: _iqa,
                       note="Asks the model for a 0..1 quality judgement. Slow, subjective and "
                            "not comparable across models — a fallback, not a metric.",
                       settings=_settings("iqa", "Ask for JSON with a 0..1 'quality'."), **common)

    host.logger.info("vlm module: registered detect / classify / tag / describe / iqa providers")