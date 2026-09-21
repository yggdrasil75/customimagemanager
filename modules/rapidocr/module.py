"""
RapidOCR provider — ONNX OCR, models bundled with the wheel (fast, CPU-friendly).
"""
import model_registry
from optional_deps import optional_import

# `rapidocr` (3.x) is the current package; `rapidocr` is its
# retired 1.x name. Both run on onnxruntime's CPU provider — no CUDA needed.
RapidOCR, _HAVE = optional_import("rapidocr", attr="RapidOCR")
_LEGACY = False
if not _HAVE:
    RapidOCR, _HAVE = optional_import("rapidocr", attr="RapidOCR")
    _LEGACY = bool(_HAVE)
AVAILABLE = bool(_HAVE)
UNAVAILABLE_REASON = "pip install rapidocr"

MANIFEST = {
    "id":          "rapidocr",
    "name":        "RapidOCR",
    "version":     "1.0.0",
    "description": "RapidOCR (PaddleOCR models on ONNX Runtime) as an OCR provider.",
    "core":        False,
    "requires":    ["ocr"],
    "pip":         ["rapidocr"],
    "assets":      [],
}


def _reader():
    key = "ocr:rapidocr"
    def _make():
        if _LEGACY:
            return RapidOCR(intra_op_num_threads=1, inter_op_num_threads=1)
        return RapidOCR(params={"Global.log_level": "warning",
                                "EngineConfig.onnxruntime.intra_op_num_threads": 1,
                                "EngineConfig.onnxruntime.inter_op_num_threads": 1})
    model_registry.register(key, _make, cost_mb=120, gpu=False)
    return model_registry.acquire(key)


def register(host):
    line = host.get_service("ocr")["line"]

    def _loader():
        rd = _reader()
        if rd is None:
            raise RuntimeError("RapidOCR failed to initialise")

        def run(img_bgr, *a, **k):
            H, W = img_bgr.shape[:2]
            out = rd(img_bgr)
            if _LEGACY:
                res = out[0] or []
            else:   # 3.x: RapidOCROutput with parallel boxes / txts / scores
                res = list(zip(out.boxes, out.txts, out.scores)) if out.boxes is not None else []
            lines = []
            for box, text, score in res:
                xs = [p[0] for p in box]; ys = [p[1] for p in box]
                lines.append(line(text, score, min(xs), min(ys), max(xs), max(ys), W, H))
            return {"text": " ".join(l["text"] for l in lines), "lines": lines}
        return run

    host.provide_model(
        "ocr", "rapidocr", label="RapidOCR", family="PaddleOCR", speed="fast",
        supports_conf=False,
        note="ONNX PaddleOCR models bundled with the wheel: no downloads, fast on CPU, "
             "solid on printed text and Latin/CJK.",
        loader=_loader, transform=None, available=lambda: True, reason="", cost_mb=120)
    host.logger.info("rapidocr module: registered ocr provider")