"""
RapidOCR provider — ONNX OCR, models bundled with the wheel (fast, CPU-friendly).
"""
import model_registry
from optional_deps import optional_import

RapidOCR, _HAVE = optional_import("rapidocr_onnxruntime", attr="RapidOCR")
AVAILABLE = bool(_HAVE)
UNAVAILABLE_REASON = "pip install rapidocr_onnxruntime"

MANIFEST = {
    "id":          "rapidocr",
    "name":        "RapidOCR",
    "version":     "1.0.0",
    "description": "RapidOCR (PaddleOCR models on ONNX Runtime) as an OCR provider.",
    "core":        False,
    "requires":    ["ocr"],
    "pip":         ["rapidocr_onnxruntime"],
    "assets":      [],
}


def _reader():
    key = "ocr:rapidocr"
    model_registry.register(key, (lambda: RapidOCR(intra_op_num_threads=1, inter_op_num_threads=1)),
                            cost_mb=120, gpu=False)
    return model_registry.acquire(key)


def register(host):
    line = host.get_service("ocr")["line"]

    def _loader():
        rd = _reader()
        if rd is None:
            raise RuntimeError("RapidOCR failed to initialise")

        def run(img_bgr, *a, **k):
            H, W = img_bgr.shape[:2]
            res, _ = rd(img_bgr)
            lines = []
            for box, text, score in (res or []):
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