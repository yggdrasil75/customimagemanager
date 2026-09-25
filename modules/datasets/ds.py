"""
Dataset download + normalisation (no app state; module.py wires it in).
======================================================================
Targets (queued through the fetch module like any URL). Closed zoos get a
one-click entry in Settings > Datasets; open-ended hosts take a link:

    dataset:pyiqa:<name>                   IQA benchmark sets pyiqa mirrors (PYIQA below), MOS labelled
    dataset:ultralytics:<name>             any dataset YAML shipped with the installed ultralytics
    dataset:hf:<owner>/<name>              Hugging Face dataset repo      (or its https://huggingface.co/datasets/… link)
    dataset:kaggle:<owner>/<name>          Kaggle dataset                 (or its https://www.kaggle.com/datasets/… link)
    dataset:zenodo:<record id>             Zenodo record, every file      (or its https://zenodo.org/records/… link)
    dataset:<url>                          zip / tar(.gz|.bz2|.xz) / 7z / parquet / csv / image

followed by optional space-separated key=value options:

    name=<folder>    dataset folder under media/.datasets/ (default: repo / file name)
    score=<column>   label column (default: first of SCORE_COLS present)
    image=<column>   parquet image column (default: first struct-with-bytes / binary column)
    split=<text>     hf: only files whose path contains this (e.g. train)
    rev=<branch>     hf: revision (default main)

Several targets with the same name= land in the same folder (e.g. an image
zip and its scores zip). After download every archive is extracted, parquet
image columns are written out as files, and any CSV/TSV with an image-name
column + score column becomes <folder>/labels.csv ("name,score", 0..1),
which is the shape modules/iqa_train reads. pyiqa sets use their own
meta_info file and published MOS range instead of the guess.
"""
import base64
import csv
import importlib.util
import json
import os
import re
import tarfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from optional_deps import optional_import

pq, HAVE_PARQUET = optional_import("pyarrow.parquet", quiet=True)
pa_types, _ = optional_import("pyarrow.types", quiet=True)
py7zr, HAVE_7Z = optional_import("py7zr", quiet=True)
yaml, HAVE_YAML = optional_import("yaml", quiet=True)

PREFIX = "dataset:"
IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".jxl", ".avif"}
SCORE_COLS = ("mos", "mos_zscore", "score", "mean", "rating", "quality", "label", "aesthetic", "aesthetic_score")
ARCHIVE_EXTS = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".7z")
LABELS = "labels.csv"
PARQUET_LABELS = "_parquet_labels.csv"     # raw scores pulled out of parquet rows (parquets are deleted)
CHUNK = 1 << 20
_MAGIC = ((b"\xff\xd8", ".jpg"), (b"\x89PNG", ".png"), (b"GIF8", ".gif"), (b"BM", ".bmp"),
          (b"RIFF", ".webp"), (b"II*\x00", ".tif"), (b"MM\x00*", ".tif"))

# pyiqa's dataset mirror (pyiqa/data/dataset_api.py + default_dataset_configs.yml):
# name: (label, archive in PYIQA_REPO, meta csv in PYIQA_META_REPO, full-reference?,
#        image root inside the archive, MOS range or None, lower_better)
# FR meta rows are "ref,dist,mos", NR rows "name,mos". PieAPP / BAPPS are
# pairwise preferences, not MOS, so they are left out.
PYIQA_REPO = "chaofengc/IQA-PyTorch-Datasets"
PYIQA_META_REPO = "chaofengc/IQA-PyTorch-Datasets-metainfo"
PYIQA = {
    "koniq10k": ("KonIQ-10k (in-the-wild, 10k)", "koniq10k.tgz", "meta_info_KonIQ10kDataset.csv", False, "koniq10k/512x384", (1, 100), False),
    "spaq":     ("SPAQ (smartphone photos, 11k)", "spaq.tgz", "meta_info_SPAQDataset.csv", False, "SPAQ/TestImage", (1, 100), False),
    "livec":    ("LIVE Challenge (in-the-wild, 1.2k)", "live_challenge.tgz", "meta_info_LIVEChallengeDataset.csv", False, "LIVEC", (1, 100), False),
    "flive":    ("FLIVE / PaQ-2-PiQ (40k)", "flive.tgz", "meta_info_FLIVEDataset.csv", False, "FLIVE_Database/database", (0, 100), False),
    "ava":      ("AVA (aesthetics, 255k)", "ava.tgz", "meta_info_AVADataset.csv", False, "AVA_dataset/ava_images", (1, 10), False),
    "gfiqa":    ("GFIQA-20k (faces)", "gfiqa-20k.tgz", "meta_info_GFIQADataset.csv", False, "GFIQA-20k/image", None, False),
    "cgfiqa":   ("CGFIQA (faces)", "CGFIQA.zip", "meta_info_CGFIQADataset.csv", False, "CGFIQA", None, False),
    "kadid10k": ("KADID-10k (synthetic distortions)", "kadid10k.tgz", "meta_info_KADID10kDataset.csv", True, "kadid10k/images", (1, 5), False),
    "pipal":    ("PIPAL (restoration outputs)", "pipal.tar", "meta_info_PIPALDataset.csv", True, "PIPAL/Dist_Imgs", (0, 1), False),
    "tid2013":  ("TID2013 (synthetic distortions)", "tid2013.tgz", "meta_info_TID2013Dataset.csv", True, "tid2013/distorted_images", (0, 9), False),
    "tid2008":  ("TID2008 (synthetic distortions)", "tid2008.tgz", "meta_info_TID2008Dataset.csv", True, "tid2008/distorted_images", (0, 9), False),
    "csiq":     ("CSIQ (synthetic distortions)", "csiq.tgz", "meta_info_CSIQDataset.csv", True, "CSIQ/dst_imgs", (0, 1), True),
    "live":     ("LIVE IQA r2 (synthetic distortions)", "live.tgz", "meta_info_LIVEIQADataset.csv", True, "LIVEIQA_release2", (1, 100), True),
    "livem":    ("LIVE Multiply Distorted", "livem.tgz", "meta_info_LIVEMDDataset.csv", True, "LIVEmultidistortiondatabase", (1, 100), True),
}

_URL_FORMS = (
    (r"https?://huggingface\.co/datasets/([^/]+/[^/?#]+)", "hf"),
    (r"https?://(?:www\.)?kaggle\.com/datasets/([^/]+/[^/?#]+)", "kaggle"),
    (r"https?://zenodo\.org/records?/(\d+)", "zenodo"),
)


class DatasetError(RuntimeError):
    pass


# ── zoos ────────────────────────────────────────────────────────────────────

def _ultra_dir():
    try:
        spec = importlib.util.find_spec("ultralytics")
    except (ImportError, ValueError):
        return None
    d = spec and spec.submodule_search_locations and \
        os.path.join(list(spec.submodule_search_locations)[0], "cfg", "datasets")
    return d if d and os.path.isdir(d) else None


def ultralytics_zoo():
    """{name: {"label", "download", "data"}} for every YAML in the installed ultralytics
    whose `download` is a URL or an inline python script (read, not imported)."""
    d = _ultra_dir()
    if not d or not HAVE_YAML:
        return {}
    out = {}
    for fn in sorted(os.listdir(d), key=str.lower):
        if not fn.endswith(".yaml"):
            continue
        try:
            with open(os.path.join(d, fn), encoding="utf-8") as f:
                text = f.read()
            data = yaml.safe_load(text) or {}
        except Exception:
            continue
        dl = data.get("download")
        if not isinstance(dl, str) or not (dl.startswith("http") or "\n" in dl):
            continue
        desc = next((l.lstrip("# ").strip() for l in text.splitlines()
                     if l.startswith("#") and len(l) > 3 and "License" not in l
                     and not l.lstrip("# ").startswith(("Documentation", "Example"))), "")
        out[fn[:-5]] = {"label": desc or fn[:-5], "download": dl, "data": data}
    return out


def zoo():
    """What Settings > Datasets offers as one-click downloads."""
    return [
        {"id": "pyiqa", "label": "IQA benchmarks (pyiqa mirror on Hugging Face)", "labelled": True,
         "items": [{"target": f"{PREFIX}pyiqa:{k}", "name": k, "label": v[0]} for k, v in PYIQA.items()]},
        {"id": "ultralytics", "label": "Ultralytics datasets (installed package)", "labelled": False,
         "items": [{"target": f"{PREFIX}ultralytics:{k}", "name": dest_name(v), "label": f"{k}: {v['label']}"}
                   for k, v in ultralytics_zoo().items()]},
    ]


def dest_name(u):
    """Folder an ultralytics dataset lands in: its YAML `path` (the name its zips/scripts use)."""
    return os.path.basename(str(u["data"].get("path") or "").rstrip("/\\")) or "dataset"


# ── target parsing ──────────────────────────────────────────────────────────

def parse_target(t):
    """'dataset:<src> k=v ...' -> {"kind", "src", "name", **opts}."""
    body = (t or "").strip()
    if not body.lower().startswith(PREFIX):
        raise DatasetError("not a dataset target")
    parts = body[len(PREFIX):].split()
    if not parts:
        raise DatasetError("dataset: needs a link or <zoo>:<name>")
    src, opts = parts[0], {}
    for p in parts[1:]:
        k, eq, v = p.partition("=")
        if eq:
            opts[k.strip().lower()] = v.strip()
    for rx, kind in _URL_FORMS:
        m = re.match(rx, src)
        if m:
            src = f"{kind}:{m.group(1)}"
            break
    kind, _, ref = src.partition(":")
    if kind == "hf" or kind == "kaggle":
        ref = ref.strip("/")
        if ref.count("/") != 1:
            raise DatasetError(f"{kind}: needs <owner>/<name>")
        out = {"kind": kind, "src": ref, "name": ref.split("/")[1]}
    elif kind == "zenodo":
        if not ref.isdigit():
            raise DatasetError("zenodo: needs a record id")
        out = {"kind": kind, "src": ref, "name": f"zenodo-{ref}"}
    elif kind == "pyiqa":
        if ref not in PYIQA:
            raise DatasetError(f"unknown pyiqa dataset '{ref}' (have: {', '.join(PYIQA)})")
        out = {"kind": kind, "src": ref, "name": ref}
    elif kind == "ultralytics":
        u = ultralytics_zoo().get(ref)
        if u is None:
            raise DatasetError(f"ultralytics has no downloadable dataset '{ref}' (or is not installed)")
        out = {"kind": kind, "src": ref, "name": dest_name(u)}
    elif src.startswith(("http://", "https://")):
        base = os.path.basename(urllib.parse.urlparse(src).path) or "dataset"
        stem = base
        for ext in ARCHIVE_EXTS + (".parquet", ".csv"):
            if stem.lower().endswith(ext):
                stem = stem[:-len(ext)]; break
        out = {"kind": "url", "src": src, "name": stem or "dataset"}
    else:
        raise DatasetError(f"unknown dataset source: {src}")
    out.update(opts)
    return out


def host_of(spec):
    """Concurrency bucket: the host the bytes come from."""
    k = spec["kind"]
    if k in ("hf", "pyiqa"):
        return "huggingface.co"
    if k == "kaggle":
        return "kaggle.com"
    if k == "zenodo":
        return "zenodo.org"
    if k == "ultralytics":
        dl = ultralytics_zoo().get(spec["src"], {}).get("download", "")
        return urllib.parse.urlparse(dl).netloc if dl.startswith("http") else "ultralytics"
    return urllib.parse.urlparse(spec["src"]).netloc


# ── downloading (generators: yield per chunk so the caller can cancel) ──────

def _auth_header(url, creds):
    creds = creds or {}
    host = urllib.parse.urlparse(url).netloc
    if host.endswith("huggingface.co") and creds.get("hf"):
        return f"Bearer {creds['hf']}"
    if host.endswith("kaggle.com") and creds.get("kaggle_user") and creds.get("kaggle_key"):
        tok = base64.b64encode(f"{creds['kaggle_user']}:{creds['kaggle_key']}".encode()).decode()
        return f"Basic {tok}"
    return None


def _open(url, creds=None):
    req = urllib.request.Request(url, headers={"User-Agent": "customimagemanager-datasets"})
    auth = _auth_header(url, creds)
    if auth:
        req.add_unredirected_header("Authorization", auth)   # never forwarded to the CDN a host redirects to
    try:
        return urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        hint = ""
        if e.code in (401, 403):
            host = urllib.parse.urlparse(url).netloc
            if "huggingface.co" in host:
                hint = " (gated/private: set the Hugging Face token in Settings > Datasets)"
            elif "kaggle.com" in host:
                hint = " (set the Kaggle username + API key in Settings > Datasets, and accept the dataset's rules on kaggle.com)"
        raise DatasetError(f"HTTP {e.code} for {url}{hint}") from e


def _json(url, creds=None):
    with _open(url, creds) as r:
        return json.load(r), r.headers


def download(url, path, creds=None, size=None):
    """Stream url -> path (via .part). Skips when path exists with the expected size."""
    if os.path.exists(path) and (size is None or os.path.getsize(path) == size):
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with _open(url, creds) as r, open(tmp, "wb") as f:
        while True:
            b = r.read(CHUNK)
            if not b:
                break
            f.write(b)
            yield
    os.replace(tmp, path)


def hf_files(repo, rev="main", creds=None):
    """[(path, size)] of every file in a HF dataset repo (follows the tree API's paging)."""
    url = (f"https://huggingface.co/api/datasets/{repo}/tree/{urllib.parse.quote(rev, safe='')}"
           "?recursive=true&expand=false")
    out = []
    while url:
        rows, headers = _json(url, creds)
        out += [(e["path"], e.get("size")) for e in rows if e.get("type") == "file"]
        nxt = headers.get("Link") or ""
        url = nxt.split(";")[0].strip("<> ") if 'rel="next"' in nxt else None
    return out


def hf_url(repo, path, rev="main"):
    return (f"https://huggingface.co/datasets/{repo}/resolve/{urllib.parse.quote(rev, safe='')}/"
            + urllib.parse.quote(path))


def zenodo_files(record, creds=None):
    """[(name, url, size)] of a Zenodo record's files."""
    rec, _ = _json(f"https://zenodo.org/api/records/{record}", creds)
    files = rec.get("files") or []
    if isinstance(files, dict):                          # some API versions nest {"entries": {...}}
        files = list((files.get("entries") or {}).values())
    return [(f["key"], (f.get("links") or {}).get("content") or f["links"]["self"], f.get("size"))
            for f in files]


# ── normalisation ───────────────────────────────────────────────────────────

def _inside(root, path):
    root = os.path.realpath(root)
    return os.path.realpath(path).startswith(root + os.sep)


def _extract(path):
    """Extract one archive next to itself; refuse members that escape the folder."""
    d, low = os.path.dirname(path), path.lower()
    if low.endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            z.extractall(d)                               # zipfile strips ../ and absolute names
    elif low.endswith(".7z"):
        if not HAVE_7Z:
            raise DatasetError("py7zr not installed; cannot extract .7z")
        with py7zr.SevenZipFile(path) as z:
            if any(not _inside(d, os.path.join(d, n)) for n in z.getnames()):
                raise DatasetError(f"unsafe paths in {path}")
            z.extractall(d)
    else:
        with tarfile.open(path) as t:
            if hasattr(tarfile, "data_filter"):
                t.extractall(d, filter="data")
            else:
                if any(not _inside(d, os.path.join(d, m.name)) for m in t.getmembers()):
                    raise DatasetError(f"unsafe paths in {path}")
                t.extractall(d)


def extract_all(root):
    """Extract every archive under root (repeat for archives inside archives), deleting each after."""
    done = set()
    while True:
        todo = [os.path.join(dp, f) for dp, _dn, fns in os.walk(root) for f in fns
                if f.lower().endswith(ARCHIVE_EXTS) and os.path.join(dp, f) not in done]
        if not todo:
            return len(done)
        for p in todo:
            _extract(p)
            done.add(p)
            os.remove(p)
            yield


def _ext_of(data, name=""):
    e = os.path.splitext(name or "")[1].lower()
    if e in IMG_EXTS:
        return e
    for magic, ext in _MAGIC:
        if data[:len(magic)] == magic:
            return ext
    return ".jpg"


def _image_col(schema, want=None):
    if want:
        return want if want in schema.names else None
    for f in schema:
        t = f.type
        if pa_types.is_struct(t) and any(t.field(i).name == "bytes" for i in range(t.num_fields)):
            return f.name
        if pa_types.is_binary(t) or pa_types.is_large_binary(t):
            return f.name
    return None


def _score_col(names, want=None):
    low = {n.lower(): n for n in names}
    if want:
        return low.get(want.lower())
    return next((low[c] for c in SCORE_COLS if c in low), None)


def parquet_to_images(root, image=None, score=None):
    """Write every parquet's image column out as files under root/images/, append
    (name, raw score) to root/_parquet_labels.csv, then delete the parquet."""
    files = sorted(os.path.join(dp, f) for dp, _dn, fns in os.walk(root) for f in fns
                   if f.lower().endswith(".parquet"))
    if not files:
        return 0
    if not HAVE_PARQUET:
        raise DatasetError("pyarrow not installed; cannot unpack parquet datasets (pip install pyarrow)")
    img_dir = os.path.join(root, "images")
    os.makedirs(img_dir, exist_ok=True)
    n = 0
    with open(os.path.join(root, PARQUET_LABELS), "a", newline="", encoding="utf-8") as lf:
        w = csv.writer(lf)
        for p in files:
            pf = pq.ParquetFile(p)
            icol = _image_col(pf.schema_arrow, image)
            if icol is None:
                continue                                  # a metadata-only parquet; leave it
            scol = _score_col(pf.schema_arrow.names, score)
            stem = os.path.splitext(os.path.basename(p))[0]
            i = 0
            for batch in pf.iter_batches(batch_size=256, columns=[icol] + ([scol] if scol else [])):
                imgs = batch.column(0).to_pylist()
                scores = batch.column(1).to_pylist() if scol else [None] * len(imgs)
                for v, s in zip(imgs, scores):
                    i += 1
                    data, src = (v.get("bytes"), v.get("path")) if isinstance(v, dict) else (v, None)
                    if not data:
                        continue
                    base = os.path.basename(src) if src else ""
                    name = base if base and not os.path.exists(os.path.join(img_dir, base)) \
                        else f"{stem}_{i:07d}{_ext_of(data, base)}"
                    with open(os.path.join(img_dir, name), "wb") as f:
                        f.write(data)
                    n += 1
                    if s is not None:
                        w.writerow([name, s])
                yield
            os.remove(p)
    return n


def index_images(root):
    idx = {}
    for dp, _dn, fns in os.walk(root):
        for fn in fns:
            if os.path.splitext(fn)[1].lower() in IMG_EXTS:
                idx.setdefault(fn, fn)
                idx.setdefault(os.path.splitext(fn)[0], fn)
    return idx


def _csv_rows(path, idx, score=None):
    """{image basename: raw score} from one CSV/TSV with a header, or {}."""
    try:
        with open(path, encoding="utf-8", errors="replace", newline="") as f:
            first = f.readline(); f.seek(0)
            rows = list(csv.reader(f, delimiter="\t" if "\t" in first else ","))
    except OSError:
        return {}
    if len(rows) < 2:
        return {}
    head, body = rows[0], rows[1:]
    scol = _score_col(head, score)
    if scol is None:
        if path.endswith(PARQUET_LABELS):                 # headerless name,score
            head, body, scol = ["name", "score"], rows, "score"
        else:
            return {}
    si = head.index(scol)
    sample = body[:50]

    def hits(ci):
        return sum(1 for r in sample if ci < len(r) and
                   (idx.get(os.path.basename(r[ci].strip())) or idx.get(r[ci].strip())))
    ni = max((ci for ci in range(len(head)) if ci != si), key=hits, default=None)
    if ni is None or hits(ni) * 2 < len(sample):
        return {}
    out = {}
    for r in body:
        if max(ni, si) >= len(r):
            continue
        key = r[ni].strip()
        name = idx.get(os.path.basename(key)) or idx.get(key)
        try:
            if name:
                out[name] = float(r[si])
        except ValueError:
            continue
    return out


def build_labels(root, score=None):
    """Write root/labels.csv (name, score 0..1) from every labelled CSV/TSV under root.
    Returns its path, or None when nothing carried labels."""
    idx = index_images(root)
    ours = os.path.join(root, LABELS)
    raw = {}
    for dp, _dn, fns in os.walk(root):
        for fn in sorted(fns):
            p = os.path.join(dp, fn)
            if p != ours and fn.lower().endswith((".csv", ".tsv")):
                raw.update(_csv_rows(p, idx, score))
    if not raw:
        return None
    lo, hi = min(raw.values()), max(raw.values())
    span = (hi - lo) or 1.0      # ponytail: min-max per dataset; absolute scale across datasets is lost
    with open(ours, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["name", "score"])
        for n, s in sorted(raw.items()):
            w.writerow([n, round((s - lo) / span, 6)])
    return ours


def find_ava(root):
    for dp, _dn, fns in os.walk(root):
        for fn in fns:
            if fn.lower() == "ava.txt":
                return os.path.join(dp, fn)
    return None


def normalise(root, spec):
    """Extract, unpack parquet, build labels. Generator (yields for cancel); returns labels path|None."""
    yield from extract_all(root)
    yield from parquet_to_images(root, spec.get("image"), spec.get("score"))
    return build_labels(root, spec.get("score")) or find_ava(root)



def pyiqa_labels(root, name, meta_path):
    """root/labels.csv from a pyiqa meta_info CSV: paths relative to root, MOS
    mapped to 0..1 with the published range (flipped when lower is better).
    Returns the labels path or None."""
    _label, _arc, _meta, fr, img_root, rng, lower = PYIQA[name]
    with open(meta_path, encoding="utf-8", errors="replace", newline="") as f:
        rows = list(csv.reader(f))[1:]
    ni = 1 if fr else 0
    raw = {}
    for r in rows:
        if len(r) <= ni + 1:
            continue
        p = os.path.join(root, img_root, r[ni].strip())
        try:
            if os.path.isfile(p):
                raw[os.path.relpath(p, root).replace(os.sep, "/")] = float(r[ni + 1])
        except ValueError:
            continue
    if not raw:
        return None
    lo, hi = rng if rng else (min(raw.values()), max(raw.values()))
    if min(raw.values()) < lo or max(raw.values()) > hi:  # meta not on the published scale: use what is there
        lo, hi = min(raw.values()), max(raw.values())
    span = (hi - lo) or 1.0
    out = os.path.join(root, LABELS)
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["name", "score"])
        for n, s in sorted(raw.items()):
            v = (s - lo) / span
            w.writerow([n, round(1.0 - v if lower else v, 6)])
    return out


def _run_ultralytics_script(script, data, root):
    """Run a YAML's inline download script exactly as ultralytics' check_det_dataset
    does (exec with `yaml` = the parsed YAML, path = our folder). Its downloads
    already unzip, so leftover archives it drops (in root or root's parent) are removed.
    Generator (one yield at the end): the script itself can't be cancelled mid-way."""
    parent = os.path.dirname(root)
    before = set(os.listdir(parent))
    exec(script, {"yaml": {**data, "path": Path(root)}})  # noqa: S102 - shipped with the installed ultralytics
    for fn in set(os.listdir(parent)) - before:
        if fn.lower().endswith(ARCHIVE_EXTS):
            os.remove(os.path.join(parent, fn))
    for dp, _dn, fns in os.walk(root):
        for fn in fns:
            if fn.lower().endswith(ARCHIVE_EXTS):
                os.remove(os.path.join(dp, fn))
    yield


def _once(root, key, gen):
    """Run a download generator unless the marker for key exists (archives are deleted
    after extraction, so their absence can't mean 'not fetched')."""
    mark = os.path.join(root, ".fetched_" + re.sub(r"[^\w.-]", "_", key) + ".done")
    if os.path.exists(mark):
        return
    yield from gen
    open(mark, "w").close()


def fetch(spec, root, creds=None):
    """Download spec into root, then normalise. Generator; returns labels path|None."""
    os.makedirs(root, exist_ok=True)
    kind, src = spec["kind"], spec["src"]
    if kind == "pyiqa":
        _l, arc, meta, *_ = PYIQA[src]
        meta_path = os.path.join(root, "_pyiqa_meta_info.txt")   # not .csv: the generic label scan must skip it
        yield from download(hf_url(PYIQA_META_REPO, meta), meta_path, creds)
        yield from _once(root, arc, download(hf_url(PYIQA_REPO, arc), os.path.join(root, arc), creds))
        yield from extract_all(root)
        return pyiqa_labels(root, src, meta_path)
    if kind == "ultralytics":
        u = ultralytics_zoo()[src]
        dl = u["download"]
        if dl.startswith("http"):
            base = os.path.basename(urllib.parse.urlparse(dl).path)
            yield from _once(root, base, download(dl, os.path.join(root, base), creds))
        else:
            yield from _once(root, "script", _run_ultralytics_script(dl, u["data"], root))
    elif kind == "hf":
        rev = spec.get("rev") or "main"
        split = spec.get("split")
        files = [(p, s) for p, s in hf_files(src, rev, creds)
                 if not os.path.basename(p).startswith(".") and (not split or split in p)]
        if not files:
            raise DatasetError(f"no files in hf:{src}" + (f" matching split={split}" if split else ""))
        for p, size in files:
            dst = os.path.join(root, p)
            if _inside(root, dst):
                yield from download(hf_url(src, p, rev), dst, creds, size)
    elif kind == "zenodo":
        for key, url, size in zenodo_files(src, creds):
            dst = os.path.join(root, key)
            if _inside(root, dst):
                yield from _once(root, key, download(url, dst, creds, size))
    elif kind == "kaggle":
        url = f"https://www.kaggle.com/api/v1/datasets/download/{src}"
        yield from _once(root, "kaggle", download(url, os.path.join(root, spec["name"] + ".zip"), creds))
    else:
        base = os.path.basename(urllib.parse.urlparse(src).path) or "download.bin"
        yield from _once(root, base, download(src, os.path.join(root, base), creds))
    return (yield from normalise(root, spec))