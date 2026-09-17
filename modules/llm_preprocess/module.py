"""
LLM image preprocessing module.
======================================================================
Downscale and/or letterbox every image before it is handed to a vision
LLM (Smart Tag pipeline, prompted detection, AI actions). Small or older
local models choke on oversized inputs and odd aspect ratios; this makes
both configurable and keeps the transform out of the core.

Hooks the core's `llm.image` event: the core emits the BGR image it is
about to encode and uses whatever this handler returns.

Settings live behind this module's ⚙ Settings button (Modules tab).
"""
from . import preprocess as pp

MANIFEST = {
    "id":          "llm_preprocess",
    "name":        "LLM image preprocessing",
    "version":     "1.0.0",
    "description": "Compress and/or pad images to model-friendly sizes and aspect "
                   "ratios before they go to the vision LLM.",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "assets":      [],
}


def register(host):
    d = pp.DEFAULT
    host.add_config_key("llm_pp_compress", default=d["compress"]["enabled"], validate=bool)
    host.add_config_key("llm_pp_max_side", default=d["compress"]["max_side"],
                        validate=lambda v: max(64, int(v)))
    host.add_config_key("llm_pp_interp", default=d["compress"]["interp"],
                        validate=lambda v: v if v in pp.INTERP_METHODS else "area")
    host.add_config_key("llm_pp_pad", default=d["pad"]["enabled"], validate=bool)
    host.add_config_key("llm_pp_fill", default=d["pad"]["fill"],
                        validate=lambda v: v if v in ("black", "white", "noise") else "black")
    host.add_config_key("llm_pp_ratios", default=",".join(d["pad"]["ratios"]),
                        validate=lambda v: ",".join(r for r in str(v).replace(" ", "").split(",")
                                                    if r in pp.VALID_RATIOS))
    host.add_settings_field(key="llm_pp_compress", label="Compress (downscale)", kind="toggle",
                            pane="module")
    host.add_settings_field(key="llm_pp_max_side", label="Max longest side (px)", kind="number",
                            pane="module")
    host.add_settings_field(key="llm_pp_interp", label="Interpolation", kind="select", pane="module",
                            options=[{"value": k, "label": k + (" (best downscale)" if k == "area" else "")}
                                     for k in pp.INTERP_METHODS])
    host.add_settings_field(key="llm_pp_pad", label="Pad to an allowed aspect ratio", kind="toggle",
                            pane="module")
    host.add_settings_field(key="llm_pp_fill", label="Pad fill", kind="select", pane="module",
                            options=[{"value": v, "label": l} for v, l in
                                     (("black", "black"), ("white", "white"), ("noise", "random noise"))])
    host.add_settings_field(key="llm_pp_ratios", label="Allowed ratios (comma list)", kind="text",
                            pane="module",
                            help="Any of: " + ", ".join(pp.VALID_RATIOS) + ". Snaps to the nearest.")

    def _cfg():
        c = host.config
        return {"compress": {"enabled": bool(c.get("llm_pp_compress")),
                             "max_side": int(c.get("llm_pp_max_side") or 1024),
                             "interp": c.get("llm_pp_interp") or "area"},
                "pad": {"enabled": bool(c.get("llm_pp_pad")), "fill": c.get("llm_pp_fill") or "black",
                        "ratios": [r for r in str(c.get("llm_pp_ratios") or "").split(",") if r]}}

    host.on("llm.image", lambda image: pp.preprocess(image, _cfg()))

    def _migrate():
        old = host.config.pop("llm_preprocess", None)
        if not isinstance(old, dict):
            return
        cmp_, pad = old.get("compress") or {}, old.get("pad") or {}
        host.config.update({"llm_pp_compress": bool(cmp_.get("enabled")),
                            "llm_pp_max_side": int(cmp_.get("max_side") or 1024),
                            "llm_pp_interp": cmp_.get("interp") or "area",
                            "llm_pp_pad": bool(pad.get("enabled")), "llm_pp_fill": pad.get("fill") or "black",
                            "llm_pp_ratios": ",".join(pad.get("ratios") or [])})
        host.save_config()
    host.on_startup(_migrate)
    host.logger.info("llm_preprocess module: hooked llm.image")