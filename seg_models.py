"""
seg_models.py
=============

Segmentation-model registry for the two distinct jobs the app does with masks:

  SAM family   the "AI tools" segmenter — the interactive/pipeline mask engine
               that turns a box or a point into a pixel mask (which mask_svg.py
               then stores as normalized SVG paths). Used by the pipeline, the
               manual "segment this region" tool, SAM region proposals, etc.
               Options: SAM 3.1, SAM 2.1 (several sizes), MobileSAM, FastSAM.

  YOLO-seg     the "background segmentation" detector — a class-aware instance
               segmenter run in bulk to grab people / object bounds (person,
               and the rest of the COCO-ish classes the weights were trained on)
               without a prompt. Options: yolov26{n,s,m,l,x}-seg by default,
               plus anything the user drops in models/seg/yolo.

Both registries follow the same shape as iqa.MODELS so the settings UI treats
them identically: a list of dicts with a stable `id`, a human `label`, a `speed`
badge, a one-line `note`, and a runtime `available`/`reason` pair the UI uses to
grey out entries whose weights or deps are missing.

DISCOVERY
---------
Beyond the built-in entries, we scan:

    models/seg/sam    -> extra SAM-compatible checkpoints (*.pt, *.pth)
    models/seg/yolo   -> extra YOLO-seg weights (*.pt)

Discovered files appear in the dropdown under a "custom" group, keyed by their
filename, so a user can point at a checkpoint we don't ship a spec for.

WHAT TO SEGMENT (class filter)
------------------------------
A YOLO-seg model reports every class it was trained on (person, car, ... and,
for a domain model, things like "scalpel"). Most of those we don't want as
regions. `class_catalog()` returns a model's trained class names so the settings
UI can present checkboxes, and `wanted_class_ids()` maps the user's saved
selection back to the integer ids the detector filters on. Empty selection =
"keep everything the model knows", so the feature is opt-in and never silently
drops classes a user didn't deselect.

DEGRADES
--------
Nothing here loads a heavy model; it only inspects the environment. Every
function is exception-safe and returns a sane empty/fallback value, so a missing
torch / ultralytics / checkpoint greys out an option instead of crashing the
settings page.
"""

import os
import glob
import threading
from functools import lru_cache

# ── where models live ────────────────────────────────────────────────────────
# Mirrors manager.MODELS_DIR ("models"); overridable for tests. The two subdirs
# are created lazily on first discovery so a fresh checkout without them is fine.
MODELS_DIR = os.environ.get("CIM_MODELS_DIR", "models")
SEG_DIR = os.path.join(MODELS_DIR, "seg")
SAM_DIR = os.path.join(SEG_DIR, "sam")
YOLO_DIR = os.path.join(SEG_DIR, "yolo")

# Checkpoint extensions we recognise per family.
_SAM_EXTS = (".pt", ".pth")
_YOLO_EXTS = (".pt",)


# ── dependency probes (cheap, cached) ─────────────────────────────────────────
def _have(mod):
    try:
        __import__(mod)
        return True
    except Exception:
        return False


_HAVE_TORCH = _have("torch")
_HAVE_ULTRALYTICS = _have("ultralytics")


# ════════════════════════════════════════════════════════════════════════════
# SAM family registry — the AI-tools segmenter
# ════════════════════════════════════════════════════════════════════════════
# Each entry:
#   id        stable key persisted in settings
#   label     dropdown text
#   family    "sam3" | "sam2" | "mobile_sam" | "fastsam" — selects the loader
#             (SAM vs FastSAM) and, for display, the variant
#   size      variant tag within a family ("", "t","s","b","l") for the sizes
#   speed     "fast" | "balanced" | "accurate"  badge (rough CPU-time class)
#   weights   ultralytics model name (auto-downloaded on first use) OR a file
#             the user drops in models/seg/sam
#   note      one-liner under the dropdown
#
# All families run through ultralytics (`SAM` / `FastSAM`) — no separate SAM
# package is needed, just the model weights, which ultralytics fetches by name.
# SAM 2.1 ships in several sizes; we expose them so a big GPU can pick 'large'
# while a laptop stays on 'tiny', mirroring the YOLO/pose size knobs.
SAM_MODELS = [
    {
        "id": "sam3", "label": "SAM 3", "family": "sam3", "size": "",
        "speed": "accurate", "weights": "sam3.pt",
        "note": "Newest Segment Anything (native text prompts). Needs an "
                "ultralytics build with SAM3SemanticPredictor. The weight isn't "
                "auto-fetched by ultralytics, so it's pulled from HuggingFace "
                "into models/seg/sam/ on first use (or pre-fetch it with "
                "'Download SAM3').",
    },
    {
        "id": "sam2.1_t", "label": "SAM 2.1 · tiny", "family": "sam2",
        "size": "t", "speed": "fast", "weights": "sam2.1_t.pt",
        "note": "SAM 2.1 tiny — fastest 2.1 variant, good for bulk/CPU use.",
    },
    {
        "id": "sam2.1_s", "label": "SAM 2.1 · small", "family": "sam2",
        "size": "s", "speed": "balanced", "weights": "sam2.1_s.pt",
        "note": "SAM 2.1 small — a step up in quality over tiny.",
    },
    {
        "id": "sam2.1_b", "label": "SAM 2.1 · base+", "family": "sam2",
        "size": "b", "speed": "balanced", "weights": "sam2.1_b.pt",
        "note": "SAM 2.1 base-plus — balanced default for a mid GPU.",
    },
    {
        "id": "sam2.1_l", "label": "SAM 2.1 · large", "family": "sam2",
        "size": "l", "speed": "accurate", "weights": "sam2.1_l.pt",
        "note": "SAM 2.1 large — highest-quality 2.1 masks, GPU recommended.",
    },
    {
        "id": "mobile_sam", "label": "MobileSAM", "family": "mobile_sam",
        "size": "", "speed": "fast", "weights": "mobile_sam.pt",
        "note": "Tiny distilled SAM. Runs on CPU in real time; lower fidelity "
                "than full SAM but ideal for quick/interactive masking.",
    },
    {
        "id": "fastsam_s", "label": "FastSAM-s", "family": "fastsam",
        "size": "s", "speed": "fast", "weights": "FastSAM-s.pt",
        "note": "YOLO-based 'segment anything'. Very fast on CPU/GPU; coarser "
                "masks. Good throughput fallback when SAM is too slow.",
    },
    {
        "id": "fastsam_x", "label": "FastSAM-x", "family": "fastsam",
        "size": "x", "speed": "balanced", "weights": "FastSAM-x.pt",
        "note": "Larger FastSAM — better masks than -s, still fast.",
    },
]

SAM_DEFAULT = "sam2.1_b"


# ════════════════════════════════════════════════════════════════════════════
# YOLO-seg registry — the background (class-aware) segmenter
# ════════════════════════════════════════════════════════════════════════════
# Same shape, minus the SAM-only 'family'. `weights` is the ultralytics model
# name (auto-downloaded on first use) or a filename under models/seg/yolo.
YOLO_SEG_MODELS = [
    {
        "id": f"yolo26{s}-seg",
        "label": f"YOLOV26{s}-seg",
        "size": s,
        "speed": {"n": "fast", "s": "fast", "m": "balanced",
                  "l": "accurate", "x": "accurate"}[s],
        "weights": f"yolo26{s}-seg.pt",
        "note": f"YOLO26 {('nano','small','medium','large','xlarge')[i]} "
                f"instance segmentation. Class-aware; used to grab people and "
                f"other trained-class bounds in bulk.",
    }
    for i, s in enumerate(("n", "s", "m", "l", "x"))
]

YOLO_SEG_DEFAULT = "yolov26n-seg"


_SAM_BY_ID = {m["id"]: m for m in SAM_MODELS}
_YOLO_BY_ID = {m["id"]: m for m in YOLO_SEG_MODELS}


# ── discovery ─────────────────────────────────────────────────────────────────
def _scan(dir_path, exts):
    """Return sorted checkpoint paths under dir_path with a matching extension.
    Never raises; returns [] if the dir is absent."""
    out = []
    try:
        for ext in exts:
            out.extend(glob.glob(os.path.join(dir_path, "**", "*" + ext),
                                  recursive=True))
    except Exception:
        return []
    return sorted(set(out))


def _custom_entry(path, family=None):
    """Build a registry-shaped dict for a discovered checkpoint file. All
    families load through ultralytics, so a custom entry just carries its path."""
    name = os.path.basename(path)
    d = {
        "id": f"custom:{name}", "label": f"{name} (custom)",
        "size": "", "speed": "balanced", "weights": path,
        "note": f"User checkpoint discovered in {os.path.dirname(path)}.",
        "custom": True,
    }
    if family is not None:
        d["family"] = family
    return d


def _sam_weight_path(entry):
    """Path to a SAM entry's LOCAL checkpoint, or '' if there isn't one on disk.
    Custom entries carry their full path in 'weights'; built-ins may have a
    user-dropped copy under models/seg/sam, else '' (the caller then hands the
    bare model name to ultralytics to download). '' is not an error."""
    w = entry.get("weights", "")
    if entry.get("custom"):
        return w
    local = os.path.join(SAM_DIR, w) if w else ""
    return local if (local and os.path.exists(local)) else ""


@lru_cache(maxsize=1)
def _github_assets():
    """The set of weight filenames ultralytics can auto-download, resolved once.
    Empty set (treated as 'unknown, be optimistic') if it can't be read."""
    try:
        from ultralytics.utils.downloads import GITHUB_ASSETS_NAMES
        return set(GITHUB_ASSETS_NAMES)
    except Exception:
        return set()


def _known_download_asset(name):
    """True if `name` is a bare weight filename ultralytics can auto-download
    from its GitHub assets. Used to tell 'will fetch on first use' apart from
    'ultralytics has no such asset and it isn't on disk' (which would blow up as
    a FileNotFoundError at load time). If the asset list can't be read we
    optimistically return True (fall back to old behaviour)."""
    assets = _github_assets()
    if not assets:
        return True
    return os.path.basename(name) in assets


@lru_cache(maxsize=1)
def _have_sam3_code():
    """True if this ultralytics ships the SAM3 predictor. SAM3 isn't loaded via
    the generic SAM() wrapper or the GitHub-asset auto-download; it needs
    SAM3SemanticPredictor and a manually-provided checkpoint. Probed once."""
    try:
        from ultralytics.models.sam import SAM3SemanticPredictor  # noqa: F401
        return True
    except Exception:
        return False


def _sam_available(entry):
    """(available, reason) for a SAM entry. Every family runs through
    ultralytics, which downloads a built-in weight by name on first use — but
    only if that name is actually one of ultralytics' assets. A name it doesn't
    know (e.g. 'sam3.pt' on a build without SAM3) is NOT downloadable and, absent
    a local copy, would fail at load; report it unavailable so the UI greys it
    out instead of silently no-opping. A custom checkpoint just needs its file."""
    if not _HAVE_ULTRALYTICS:
        return False, "pip install ultralytics"
    if entry.get("custom"):
        wp = _sam_weight_path(entry)
        if wp and not os.path.exists(wp):
            return False, f"checkpoint not found: {wp}"
        return True, ""
    if entry.get("family") == "sam3":
        if not _have_sam3_code():
            return False, ("this ultralytics build has no SAM3 "
                           "(needs SAM3SemanticPredictor)")
        if not _sam_weight_path(entry):
            return True, "will download sam3.pt from HuggingFace on first use"
        return True, ""
    # built-in: OK if the user dropped a local copy, else it must be a name
    # ultralytics can fetch.
    if _sam_weight_path(entry):
        return True, ""
    w = entry.get("weights", "")
    if not _known_download_asset(w):
        return False, (f"{w} isn't available in this ultralytics build "
                       f"(drop it in {SAM_DIR} to use it)")
    return True, ""


def _yolo_available(entry):
    if not _HAVE_ULTRALYTICS:
        return False, "pip install ultralytics"
    return True, ""


def list_sam_models():
    """SAM-family registry (built-ins + discovered) for the settings dropdown.
    Each entry: id,label,family,size,speed,note,available,reason,custom.
    Never raises."""
    out = []
    for m in SAM_MODELS:
        avail, why = _sam_available(m)
        out.append({
            "id": m["id"], "label": m["label"], "family": m["family"],
            "size": m["size"], "speed": m["speed"], "note": m["note"],
            "available": bool(avail), "reason": why, "custom": False,
        })
    for p in _scan(SAM_DIR, _SAM_EXTS):
        # skip files that already back a built-in entry (same basename)
        if any(os.path.basename(p) == os.path.basename(m.get("weights", ""))
               for m in SAM_MODELS):
            continue
        e = _custom_entry(p, family="sam2")
        avail, why = _sam_available(e)
        e.update({"available": bool(avail), "reason": why})
        out.append(e)
    return out


def list_yolo_seg_models():
    """YOLO-seg registry (built-ins + discovered) for the background-seg
    dropdown. Never raises."""
    out = []
    for m in YOLO_SEG_MODELS:
        avail, why = _yolo_available(m)
        out.append({
            "id": m["id"], "label": m["label"], "size": m["size"],
            "speed": m["speed"], "note": m["note"],
            "available": bool(avail), "reason": why, "custom": False,
        })
    for p in _scan(YOLO_DIR, _YOLO_EXTS):
        e = _custom_entry(p)
        avail, why = _yolo_available(e)
        e.update({"available": bool(avail), "reason": why})
        out.append(e)
    return out


# ── active-selection helpers (state lives in manager; these just validate) ────
def sam_info(model_id):
    """Registry entry for a SAM id (built-in or discovered), or the default's
    entry if the id is unknown. Discovered entries are resolved by re-scanning."""
    if model_id in _SAM_BY_ID:
        return _SAM_BY_ID[model_id]
    for e in list_sam_models():
        if e["id"] == model_id:
            return e
    return _SAM_BY_ID[SAM_DEFAULT]


def yolo_seg_info(model_id):
    if model_id in _YOLO_BY_ID:
        return _YOLO_BY_ID[model_id]
    for e in list_yolo_seg_models():
        if e["id"] == model_id:
            return e
    return _YOLO_BY_ID[YOLO_SEG_DEFAULT]


def resolve_sam_id(model_id):
    """Coerce a persisted id to a valid one: keep it if known/discovered, else
    fall back to the default. Returns the id to actually use."""
    if model_id in _SAM_BY_ID:
        return model_id
    if any(e["id"] == model_id for e in list_sam_models()):
        return model_id
    return SAM_DEFAULT


def resolve_yolo_seg_id(model_id):
    if model_id in _YOLO_BY_ID:
        return model_id
    if any(e["id"] == model_id for e in list_yolo_seg_models()):
        return model_id
    return YOLO_SEG_DEFAULT


def sam_weights_path(model_id):
    """Filesystem path to the checkpoint for a SAM id (or '' if none/known-by-
    download). Callers hand this to the SAM loader."""
    return _sam_weight_path(sam_info(model_id))


def sam_weights_ref(model_id):
    """The path to hand ultralytics for a SAM id. Built-ins point at
    models/seg/sam/<name> (dir created) so ultralytics' auto-download lands in
    the discovery folder rather than the cwd — matching the YOLO-seg convention
    and the normal loader's use of MODELS_DIR. A discovered/custom file keeps its
    own full path."""
    entry = sam_info(model_id)
    w = entry.get("weights", "")
    if entry.get("custom") or not w:
        return w
    try:
        os.makedirs(SAM_DIR, exist_ok=True)
    except Exception:
        pass
    return os.path.join(SAM_DIR, w)


# ── SAM3 weight fetch (HuggingFace; ultralytics can't auto-download it) ────────
# SAM3's checkpoint isn't a GitHub asset, so we grab it from a HuggingFace repo
# on explicit user action and drop it at models/seg/sam/sam3.pt — the path
# _sam_available()/_get_sam() look for. Default repo is overridable via env.
SAM3_HF_REPO = os.environ.get("CIM_SAM3_HF_REPO", "AEmotionStudio/sam3.1")
_CKPT_EXTS = (".pt", ".pth", ".safetensors")


def sam3_present():
    """True if models/seg/sam/sam3.pt is already on disk."""
    return os.path.exists(os.path.join(SAM_DIR, "sam3.pt"))


def _hf_list_files(repo):
    """Filenames in a HuggingFace model repo, via the public API. [] on failure
    (offline, private, rate-limited). No token needed for a public repo."""
    import json
    import urllib.request
    url = f"https://huggingface.co/api/models/{repo}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            data = json.load(r)
        return [s.get("rfilename", "") for s in data.get("siblings", [])]
    except Exception:
        return []


def _pick_sam3_checkpoint(files):
    """Choose the checkpoint filename to pull from the repo listing: prefer a
    name containing 'sam3', else the first checkpoint-extension file. '' if none.
    Prefers .pt over .safetensors so ultralytics loads it directly."""
    cks = [f for f in files if f.lower().endswith(_CKPT_EXTS)]
    if not cks:
        return ""
    def rank(f):
        low = f.lower()
        return (0 if "sam3" in low else 1,
                _CKPT_EXTS.index(next(e for e in _CKPT_EXTS if low.endswith(e))))
    return sorted(cks, key=rank)[0]


def download_sam3(repo=None, token=None, progress=None):
    """Fetch the SAM3 checkpoint from HuggingFace into models/seg/sam/sam3.pt.

    Returns (ok, message). Safe to call when it's already present (no-op ok).
    Streams the file straight from the repo's resolve/main URL into our own
    models dir — no huggingface_hub dependency and no hidden ~/.cache location;
    the weight lands exactly where the loader looks. The default repo is public,
    so `token` is normally unnecessary (only added as a header if given, for a
    gated mirror). Never raises."""
    # Fast path: already have the canonical sam3.pt.
    if sam3_present():
        return True, "SAM3 weight already present."
    repo = repo or SAM3_HF_REPO
    try:
        os.makedirs(SAM_DIR, exist_ok=True)
    except Exception as e:
        return False, f"can't create {SAM_DIR}: {e}"

    files = _hf_list_files(repo)
    fname = _pick_sam3_checkpoint(files) if files else "sam3.pt"
    if not fname:
        return False, f"no checkpoint (*.pt/.pth/.safetensors) found in {repo}."

    # The loader looks for models/seg/sam/sam3.pt. If the repo file is a .pt we
    # save it there directly. If it's a .pth/.safetensors we keep its real
    # extension (saving it as .pt would mislabel it and ultralytics could fail),
    # and report the actual path so the user knows what landed.
    if fname.lower().endswith(".pt"):
        dest = os.path.join(SAM_DIR, "sam3.pt")
    else:
        dest = os.path.join(SAM_DIR, os.path.basename(fname))
    if os.path.exists(dest):
        return True, f"SAM3 weight already present ({os.path.basename(dest)})."

    import urllib.request
    url = f"https://huggingface.co/{repo}/resolve/main/{fname}"
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    tmp = dest + ".part"
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            total = int(r.headers.get("Content-Length", 0) or 0)
            done = 0
            with open(tmp, "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if progress and total:
                        try:
                            progress(done, total)
                        except Exception:
                            pass
        os.replace(tmp, dest)
        return True, f"Downloaded {fname} from {repo}."
    except Exception as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False, f"download failed from {url}: {e}"


def yolo_weights_ref(model_id):
    """The ultralytics weights reference for a YOLO-seg id: a discovered file's
    full path, else the auto-download model name."""
    e = yolo_seg_info(model_id)
    w = e.get("weights", "")
    if e.get("custom") or not w:
        return w
    try:
        os.makedirs(YOLO_DIR, exist_ok=True)
    except Exception:
        pass
    return os.path.join(YOLO_DIR, w)


# ── "what to segment" class filter ────────────────────────────────────────────
_catalog_cache = {}
_catalog_lock = threading.Lock()


def weights_present(model_id):
    """True if the YOLO-seg weights for `model_id` are already on disk (a
    discovered file, or a built-in copy under models/seg/yolo, or already in
    ultralytics' download cache). Lets the UI distinguish 'ready' from 'needs a
    download' without triggering the fetch. Never raises."""
    try:
        ref = yolo_weights_ref(model_id)
        return bool(ref) and os.path.exists(ref)
    except Exception:
        return False


def class_catalog(model_id, download=False):
    """Ordered {id: name} of the classes a YOLO-seg model was trained on, for
    the 'what do you want segmented' checkboxes.

    download=False (default): only read the catalog if the weights are ALREADY
    present. If they're not, return {} without fetching — so a settings-page
    request never blocks on a multi-MB download. Use weights_present() to tell
    the two empty cases apart.

    download=True: load the model, fetching the weights if ultralytics needs to.
    This may block; call it from an explicit user action (the 'Download & load
    classes' button), not from an idle render.

    Cached once loaded. Returns {} if ultralytics/the weights are unavailable.
    Never raises.
    """
    if not _HAVE_ULTRALYTICS:
        return {}
    ref = yolo_weights_ref(model_id)
    with _catalog_lock:
        if ref in _catalog_cache:
            return _catalog_cache[ref]
    if not download and not weights_present(model_id):
        return {}                       # don't trigger a download on a passive read
    names = {}
    try:
        from ultralytics import YOLO
        model = YOLO(ref)
        raw = getattr(model, "names", None) or {}
        # ultralytics 'names' is {int: str}; normalise keys to int.
        names = {int(k): str(v) for k, v in raw.items()}
    except Exception:
        names = {}
    with _catalog_lock:
        _catalog_cache[ref] = names
    return names


def wanted_class_ids(model_id, selected_names):
    """Map a user's saved class-name selection to the integer class ids the
    detector filters on (ultralytics `classes=` arg).

    selected_names: list of class-name strings the user ticked. An empty list
    means 'keep everything the model knows' -> returns None (no filter), so the
    feature never silently drops classes the user didn't deselect. Unknown names
    are ignored. Returns a sorted list of ints, or None for 'all'."""
    if not selected_names:
        return None
    catalog = class_catalog(model_id)
    if not catalog:
        return None
    want = {str(n).strip().lower() for n in selected_names if str(n).strip()}
    ids = [cid for cid, name in catalog.items() if name.lower() in want]
    return sorted(ids) if ids else None