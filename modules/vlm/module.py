"""
Vision-LLM model provider.
======================================================================
Registers the configured OpenAI-compatible vision model as a provider for
'detect' as a *prompted* provider: it draws boxes for whatever a prompt
describes, which no fixed-class detector can. Prompted providers are a
foreground-only pick (pipeline / manual buttons); the background sweep is
always an unprompted model. Uses core's existing
chat plumbing (_llm_call with the box tool schema) via a lazy manager
import, so this is only a registration seam.
"""
from modules.model_broker import NoProviderError  # noqa: F401  (re-export for consumers)

MANIFEST = {
    "id":          "vlm",
    "name":        "Vision LLM (OpenAI-compatible)",
    "version":     "1.0.0",
    "description": "Uses the configured vision-capable chat model as an "
                   "open-vocabulary detector (prompted boxes).",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "assets":      [],
}


def register(host):
    def _mgr():
        import manager as m
        return m

    def _configured():
        return bool((host.config.get("oai_endpoint") or "").strip()
                    and (host.config.get("oai_model") or "").strip())

    def _detect(img_bgr, prompt="", *a, **k):
        m = _mgr()
        p = (prompt or "").strip() or "Find every distinct object."
        boxes = m._llm_call(p + "\n\nReturn bounding boxes normalised 0..1.",
                            m._to_bgr(img_bgr), "boxes") or []
        return [{"class_name": b.get("class_name", "object"), "cx": float(b["cx"]),
                 "cy": float(b["cy"]), "w": float(b["w"]), "h": float(b["h"])}
                for b in boxes]

    host.provide_model(
        "detect", "vlm", label="Vision LLM", family="LLM", prompted=True,
        speed="accurate", supports_conf=False,
        note="Open vocabulary: finds whatever you describe. Slow and API-bound; "
             "boxes are rough. Foreground only — it has nothing to run without a prompt.",
        settings=[{"key": "oai_model", "label": "Chat model", "kind": "text",
                   "help": "Vision-capable model at the OAI endpoint (AI settings)."}],
        loader=lambda: _detect, transform=None, available=_configured,
        reason="set OAI endpoint + chat model in AI settings", cost_mb=0)
    host.logger.info("vlm module: registered prompted detect provider")