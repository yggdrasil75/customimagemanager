"""
pyiqa IQA providers (torch-backed quality metrics).
======================================================================
Self-contained. Owns the pyiqa model catalog, building each metric,
running it, and normalizing to the broker's canonical {raw, quality}.
Registers one provider per model, so the user picks e.g. MUSIQ vs NIMA
from the rating settings and the broker routes to it. The scoring code
that used to live in iqa.py for these models lives here now.

Skipped entirely when pyiqa/torch aren't installed — a torch-less or
ultralight install registers no pyiqa providers.
"""

from optional_deps import optional_import

pyiqa, _HAVE_IQA = optional_import("pyiqa")
torch, _HAVE_TORCH = optional_import("torch")
cv2, _HAVE_CV2 = optional_import("cv2")
np, _ = optional_import("numpy")
import model_registry

MANIFEST = {
    "id":          "pyiqa",
    "name":        "pyiqa quality models",
    "version":     "1.1.0",
    "description": "Torch-backed no-reference quality metrics (NIQE, NIMA, "
                   "HyperIQA, DBCNN, CLIP-IQA+, MUSIQ, MANIQA, TOPIQ). Needs "
                   "pyiqa + torch; skipped on lightweight installs.",
    "core":        False,
    "requires":    [],
    "pip":         ["pyiqa", "torch"],
    "assets":      [],
}

# id -> spec. lower_better + lo/hi drive normalization; pyiqa is the metric name.
_MODELS = [
    {"id": "niqe", "label": "NIQE", "pyiqa": "niqe", "lower_better": True, "lo": 0.0, "hi": 15.0,
     "speed": "fast", "note": "Opinion-unaware NSS metric. Fast, no training bias, but like BRISQUE it only sees distortion."},
    {"id": "brisque_pyiqa", "label": "BRISQUE (pyiqa reimpl.)", "pyiqa": "brisque", "lower_better": True, "lo": 0.0, "hi": 100.0,
     "speed": "fast", "note": "Same metric as legacy BRISQUE but via pyiqa; slightly different numbers. Useful for apples-to-apples comparison."},
    {"id": "nima", "label": "NIMA (aesthetic)", "pyiqa": "nima", "lower_better": False, "lo": 1.0, "hi": 10.0,
     "speed": "balanced", "note": "Predicts the AVA mean-opinion score. Rates *aesthetics*, not just distortion — a sharp but boring photo scores low."},
    {"id": "hyperiqa", "label": "HyperIQA", "pyiqa": "hyperiqa", "lower_better": False, "lo": 0.0, "hi": 1.0,
     "speed": "balanced", "note": "Content-adaptive CNN, trained on in-the-wild photos. Good quality/cost tradeoff for a full-library scan."},
    {"id": "dbcnn", "label": "DBCNN", "pyiqa": "dbcnn", "lower_better": False, "lo": 0.0, "hi": 1.0,
     "speed": "balanced", "note": "Two-stream CNN handling both synthetic and authentic distortion. Solid, well-tested all-rounder."},
    {"id": "clipiqa", "label": "CLIP-IQA+", "pyiqa": "clipiqa+", "lower_better": False, "lo": 0.0, "hi": 1.0,
     "speed": "balanced", "note": "CLIP-based; understands image *content*, so it punishes junk and placeholders that pure distortion metrics call 'clean'."},
    {"id": "musiq", "label": "MUSIQ", "pyiqa": "musiq", "lower_better": False, "lo": 0.0, "hi": 100.0,
     "speed": "accurate", "note": "Multi-scale transformer, native resolution (no resize crop). Excellent human correlation. Wants a GPU for bulk work."},
    {"id": "maniqa", "label": "MANIQA", "pyiqa": "maniqa", "lower_better": False, "lo": 0.0, "hi": 1.0,
     "speed": "accurate", "note": "ViT-based, NTIRE 2022 NR-IQA winner. Top-tier accuracy, slowest of the set."},
    {"id": "topiq", "label": "TOPIQ", "pyiqa": "topiq_nr", "lower_better": False, "lo": 0.0, "hi": 1.0,
     "speed": "accurate", "note": "Top-down semantic-guided IQA. Among the strongest NR models; GPU recommended."},
]


def _available():
    return _HAVE_IQA and _HAVE_TORCH


def _normalize(raw, spec):
    """0..1, higher = better. Own copy (providers are self-contained)."""
    if raw is None or spec["hi"] == spec["lo"]:
        return None
    x = max(0.0, min(1.0, (float(raw) - spec["lo"]) / (spec["hi"] - spec["lo"])))
    return (1.0 - x) if spec["lower_better"] else x


def _build(spec):
    if not _available():
        return None
    try:
        dev = model_registry.device()
        metric = pyiqa.create_metric(spec["pyiqa"], device=dev)
        metric.eval()
    except Exception:
        return None

    def raw_score(img_bgr):
        if img_bgr is None:
            return None
        try:
            rgb = cv2.cvtColor(img_bgr[:, :, :3], cv2.COLOR_BGR2RGB) \
                if _HAVE_CV2 else img_bgr[:, :, ::-1]
            t = torch.from_numpy(
                np.ascontiguousarray(rgb)).permute(2, 0, 1).float() / 255.0
            with torch.no_grad():
                return float(metric(t.unsqueeze(0).to(dev)).item())
        except Exception:
            return None
    return raw_score


def _scorer(spec):
    key = f"iqa:{spec['id']}"
    model_registry.register(
        key, (lambda s=spec: _build(s)),
        cost_mb=700, gpu=model_registry.on_gpu())

    def run(img_bgr, *a, **k):
        fn = model_registry.acquire(key)
        raw = fn(img_bgr) if fn else None
        return {"raw": raw, "quality": _normalize(raw, spec)}
    return run


def register(host):
    if not _available():
        host.logger.info("pyiqa module: pyiqa/torch not installed; "
                         "registering nothing")
        return
    for spec in _MODELS:
        host.provide_model(
            "iqa", spec["id"],
            label=spec["label"], family="pyiqa", speed=spec["speed"], note=spec["note"],
            loader=(lambda s=spec: _scorer(s)),
            available=_available,
            reason="pip install pyiqa (needs torch)",
            cost_mb=700, gpu=model_registry.on_gpu())
    host.logger.info(f"pyiqa module: registered {len(_MODELS)} iqa providers")
