"""
EasyOCR provider — CRAFT + CRNN, 80+ languages, models download on first use.
"""
import model_registry
from optional_deps import optional_import

easyocr, _HAVE = optional_import("easyocr")
AVAILABLE = bool(_HAVE)
UNAVAILABLE_REASON = "pip install easyocr"

MANIFEST = {
    "id":          "easyocr",
    "name":        "EasyOCR",
    "version":     "1.0.0",
    "description": "EasyOCR (CRAFT detector + CRNN recogniser) as an OCR provider.",
    "core":        False,
    "requires":    ["ocr"],
    "pip":         ["easyocr"],
    "assets":      [],
}

_DIR = model_registry.model_dir("easyocr", "ocr")


def register(host):
    line = host.get_service("ocr")["line"]
    host.add_config_key("easyocr_langs", default="en", validate=lambda v: str(v or "en"))

    def _langs():
        return [l.strip() for l in str(host.config.get("easyocr_langs") or "en").split(",") if l.strip()]

    def _reader():
        langs = _langs()
        key = "ocr:easyocr:" + "+".join(langs)
        model_registry.register(key, (lambda ls=langs: easyocr.Reader(ls, gpu=model_registry.on_gpu(),
                                                                       model_storage_directory=_DIR)),
                                cost_mb=400, gpu=model_registry.on_gpu())
        return model_registry.acquire(key)

    def _loader():
        rd = _reader()
        if rd is None:
            raise RuntimeError("EasyOCR failed to initialise")

        def run(img_bgr, *a, **k):
            H, W = img_bgr.shape[:2]
            lines = []
            for box, text, score in rd.readtext(img_bgr):
                xs = [p[0] for p in box]; ys = [p[1] for p in box]
                lines.append(line(text, float(score), min(xs), min(ys), max(xs), max(ys), W, H))
            return {"text": " ".join(l["text"] for l in lines), "lines": lines}
        return run

    host.provide_model(
        "ocr", "easyocr", label="EasyOCR", family="EasyOCR", speed="balanced",
        supports_conf=False,
        settings=[{"key": "easyocr_langs", "label": "Languages (comma list)", "kind": "text",
                   "help": "EasyOCR language codes, e.g. en,ja. Models download into models/easyocr/ocr."}],
        note="Deep detector + recogniser with 80+ languages; slower than RapidOCR, better "
             "on handwriting and unusual scripts. Weights download on first use.",
        loader=_loader, transform=None, available=lambda: True, reason="", cost_mb=400,
        gpu=model_registry.on_gpu())
    host.logger.info("easyocr module: registered ocr provider")