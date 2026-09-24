"""
Dataset download + normalisation (no app state; module.py wires it in).
======================================================================
Targets (queued through the fetch module like any URL):

    dataset:<url>                          zip / tar(.gz|.bz2|.xz) / 7z / parquet / csv / image
    dataset:hf:<owner>/<name>              a Hugging Face dataset repo (every file)
    dataset:https://huggingface.co/datasets/<owner>/<name>   same as hf:

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
which is the shape modules/iqa_train reads.
"""
import csv
import json
import os
import tarfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from optional_deps import optional_import

pq, HAVE_PARQUET = optional_import("pyarrow.parquet", quiet=True)
pa_types, _ = optional_import("pyarrow.types", quiet=True)
py7zr, HAVE_7Z = optional_import("py7zr", quiet=True)

PREFIX = "dataset:"
IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".jxl", ".avif"}
SCORE_COLS = ("mos", "mos_zscore", "score", "mean", "rating", "quality", "label", "aesthetic", "aesthetic_score")
ARCHIVE_EXTS = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".7z")
LABELS = "labels.csv"
PARQUET_LABELS = "_parquet_labels.csv"     # raw scores pulled out of parquet rows (parquets are deleted)
CHUNK = 1 << 20
_MAGIC = ((b"\xff\xd8", ".jpg"), (b"\x89PNG", ".png"), (b"GIF8", ".gif"), (b"BM", ".bmp"),
          (b"RIFF", ".webp"), (b"II*\x00", ".tif"), (b"MM\x00*", ".tif"))


class DatasetError(RuntimeError):
    pass


# ── target parsing ──────────────────────────────────────────────────────────

def parse_target(t):
    """'dataset:<src> k=v ...' -> {"kind": "hf"|"url", "src", "name", **opts}."""
    body = (t or "").strip()
    if not body.lower().startswith(PREFIX):
        raise DatasetError("not a dataset target")
    parts = body[len(PREFIX):].split()
    if not parts:
        raise DatasetError("dataset: needs a URL or hf:<owner>/<name>")
    src, opts = parts[0], {}
    for p in parts[1:]:
        k, eq, v = p.partition("=")
        if eq:
            opts[k.strip().lower()] = v.strip()
    hf_url = "https://huggingface.co/datasets/"
    if src.startswith(hf_url):
        src = "hf:" + "/".join(src[len(hf_url):].split("/")[:2])
    if src.startswith("hf:"):
        repo = src[3:].strip("/")
        if repo.count("/") != 1:
            raise DatasetError("hf: needs <owner>/<name>")
        out = {"kind": "hf", "src": repo, "name": repo.split("/")[1]}
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
    return "huggingface.co" if spec["kind"] == "hf" else urllib.parse.urlparse(spec["src"]).netloc


# ── downloading (generators: yield per chunk so the caller can cancel) ──────

def _open(url, token=None):
    req = urllib.request.Request(url, headers={"User-Agent": "customimagemanager-datasets"})
    if token and "huggingface.co" in url:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        return urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        hint = " (gated/private: set the Hugging Face token in the Datasets module settings)" \
            if e.code in (401, 403) and "huggingface.co" in url else ""
        raise DatasetError(f"HTTP {e.code} for {url}{hint}") from e


def download(url, path, token=None, size=None):
    """Stream url -> path (via .part). Skips when path exists with the expected size."""
    if os.path.exists(path) and (size is None or os.path.getsize(path) == size):
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with _open(url, token) as r, open(tmp, "wb") as f:
        while True:
            b = r.read(CHUNK)
            if not b:
                break
            f.write(b)
            yield
    os.replace(tmp, path)


def hf_files(repo, rev="main", token=None):
    """[(path, size)] of every file in a HF dataset repo (follows the tree API's paging)."""
    url = (f"https://huggingface.co/api/datasets/{repo}/tree/{urllib.parse.quote(rev, safe='')}"
           "?recursive=true&expand=false")
    out = []
    while url:
        with _open(url, token) as r:
            out += [(e["path"], e.get("size")) for e in json.load(r) if e.get("type") == "file"]
            nxt = r.headers.get("Link") or ""
        url = nxt.split(";")[0].strip("<> ") if 'rel="next"' in nxt else None
    return out


def hf_url(repo, path, rev="main"):
    return (f"https://huggingface.co/datasets/{repo}/resolve/{urllib.parse.quote(rev, safe='')}/"
            + urllib.parse.quote(path))


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


def fetch(spec, root, token=None):
    """Download spec into root, then normalise. Generator; returns labels path|None."""
    os.makedirs(root, exist_ok=True)
    if spec["kind"] == "hf":
        rev = spec.get("rev") or "main"
        split = spec.get("split")
        files = [(p, s) for p, s in hf_files(spec["src"], rev, token)
                 if not os.path.basename(p).startswith(".") and (not split or split in p)]
        if not files:
            raise DatasetError(f"no files in hf:{spec['src']}" + (f" matching split={split}" if split else ""))
        for p, size in files:
            dst = os.path.join(root, p)
            if not _inside(root, dst):
                continue
            yield from download(hf_url(spec["src"], p, rev), dst, token, size)
    else:
        base = os.path.basename(urllib.parse.urlparse(spec["src"]).path) or "download.bin"
        mark = os.path.join(root, ".fetched_" + base + ".done")
        if not os.path.exists(mark):                      # archives are deleted after extraction
            yield from download(spec["src"], os.path.join(root, base))
            open(mark, "w").close()
    return (yield from normalise(root, spec))