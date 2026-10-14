"""! @file
@brief music_lib.py - the music module's library.
==========================================
Tables, tag read/write (mutagen), offline audio embeddings (librosa),
k-means clustering and shuffle-by. The routes, indexer and UI wiring are in
module.py; nothing here touches the app.
"""
from __future__ import annotations
import os
import struct
import numpy as np
from optional_deps import optional_import

MutagenFile, HAVE_MUTAGEN = optional_import("mutagen", attr="File")
librosa, HAVE_LIBROSA = optional_import("librosa")
KMeans, HAVE_SKLEARN = optional_import("sklearn.cluster", attr="KMeans", quiet=True)

# Containers we understand. Kept lowercase, with dot.
MUSIC_EXTS = {".mp3", ".flac", ".m4a", ".mp4", ".aac",
              ".ogg", ".oga", ".opus", ".wav", ".wma", ".aiff", ".aif"}

EMB_DIM = 62          # MFCC(20*2) + chroma(12) + contrast(7) + tempo(1) + zcr(1) + rms(1)
EMB_SIG = "librosa-v1"  # bump to invalidate every cached embedding


# -- schema --------------------------------------------------------------------
DDL = """
        CREATE TABLE IF NOT EXISTS music (
            rel_path     TEXT PRIMARY KEY,
            mtime        REAL,
            size         INTEGER,
            duration     REAL,           -- seconds
            bitrate      INTEGER,
            samplerate   INTEGER,
            channels     INTEGER,
            -- editable metadata --
            title        TEXT DEFAULT '',
            artist       TEXT DEFAULT '',
            album        TEXT DEFAULT '',
            albumartist  TEXT DEFAULT '',
            track        INTEGER,
            disc         INTEGER,
            year         TEXT DEFAULT '',
            genre        TEXT DEFAULT '',
            composer     TEXT DEFAULT '',
            comment      TEXT DEFAULT '',
            tags         TEXT DEFAULT '[]',   -- free-form user tags (JSON list)
            -- derived --
            emb          BLOB,
            emb_sig      TEXT,
            cluster      INTEGER DEFAULT -1,
            has_cover    INTEGER,        -- 1 / 0 once checked, NULL until indexed
            created      REAL
        );
        CREATE INDEX IF NOT EXISTS idx_music_artist  ON music(artist);
        CREATE INDEX IF NOT EXISTS idx_music_album   ON music(album);
        CREATE INDEX IF NOT EXISTS idx_music_cluster ON music(cluster);

        -- cached cluster labels (k chosen at run time)
        CREATE TABLE IF NOT EXISTS music_clusters (
            cluster   INTEGER PRIMARY KEY,
            label     TEXT DEFAULT '',
            size      INTEGER DEFAULT 0,
            created   REAL
        );
    """

# -- metadata (mutagen) ---------------------------------------------------------
def _first(d, *keys):
    for k in keys:
        v = d.get(k)
        if v:
            if isinstance(v, (list, tuple)):
                v = v[0]
            return str(v)
    return ""

def _split_num(s):
    """! @brief '3/12' -> 3 ; '7' -> 7 ; '' -> None"""
    if s is None:
        return None
    s = str(s).split('/')[0].strip()
    try:
        return int(s)
    except (ValueError, TypeError):
        return None

def read_audio_metadata(abs_path: str) -> dict:
    """! @brief Normalise tags from any container into one flat dict. Never raises."""
    out = {
        "title": "", "artist": "", "album": "", "albumartist": "",
        "track": None, "disc": None, "year": "", "genre": "",
        "composer": "", "comment": "",
        "duration": 0.0, "bitrate": 0, "samplerate": 0, "channels": 0,
    }
    try:
        mf = MutagenFile(abs_path, easy=True)
    except Exception:
        mf = None
    if mf is None:
        return out

    info = getattr(mf, "info", None)
    if info is not None:
        out["duration"]   = float(getattr(info, "length", 0) or 0)
        out["bitrate"]    = int(getattr(info, "bitrate", 0) or 0)
        out["samplerate"] = int(getattr(info, "sample_rate", 0) or 0)
        out["channels"]   = int(getattr(info, "channels", 0) or 0)

    t = dict(mf.tags or {})
    out["title"]       = _first(t, "title")
    out["artist"]      = _first(t, "artist")
    out["album"]       = _first(t, "album")
    out["albumartist"] = _first(t, "albumartist", "album artist")
    out["genre"]       = _first(t, "genre")
    out["composer"]    = _first(t, "composer")
    out["comment"]     = _first(t, "comment")
    out["year"]        = _first(t, "date", "year", "originaldate")[:10]
    out["track"]       = _split_num(_first(t, "tracknumber", "track"))
    out["disc"]        = _split_num(_first(t, "discnumber", "disc"))
    return out

# mapping from our flat keys to EasyID3/easy-mp4/vorbis key names mutagen accepts
_EASY_KEYS = {
    "title": "title", "artist": "artist", "album": "album",
    "albumartist": "albumartist", "genre": "genre",
    "composer": "composer", "year": "date",
}

def write_audio_metadata(abs_path: str, meta: dict) -> bool:
    """! @brief Write editable fields back into the file. Returns True on success."""
    try:
        mf = MutagenFile(abs_path, easy=True)
        if mf is None:
            return False
        if mf.tags is None:
            mf.add_tags()
        for flat, ezk in _EASY_KEYS.items():
            if flat in meta and meta[flat] is not None:
                val = str(meta[flat])
                if val == "":
                    mf.tags.pop(ezk, None)
                else:
                    mf.tags[ezk] = val
        if meta.get("track") not in (None, ""):
            mf.tags["tracknumber"] = str(meta["track"])
        if meta.get("disc") not in (None, ""):
            mf.tags["discnumber"] = str(meta["disc"])
        if meta.get("comment") is not None:
            # comment isn't in every easy profile; best-effort
            try:
                mf.tags["comment"] = str(meta["comment"])
            except Exception:
                pass
        mf.save()
        return True
    except Exception:
        return False

# -- cover art -------------------------------------------------------------------
# Folder images a ripper / tagger leaves beside the tracks, best first.
_FOLDER_COVERS = ("cover", "folder", "front", "album", "albumart", "albumartsmall")
_IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".jxl", ".avif")
# description formats whose text is read straight into the API reply
_TEXT_EXTS = (".txt", ".md", ".nfo")


def _sniff_mime(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:3] == b"GIF":
        return "image/gif"
    return "application/octet-stream"


def _embedded_cover(abs_path: str):
    """! @brief (bytes, mime) of the art embedded in the file, or None. The front
    cover is preferred where the container says which picture is which."""
    try:
        mf = MutagenFile(abs_path)
    except Exception:
        return None
    if mf is None:
        return None
    pics = []
    for p in getattr(mf, "pictures", None) or []:            # FLAC
        pics.append((getattr(p, "type", 0), p.data))
    tags = getattr(mf, "tags", None)
    if tags is not None:
        try:
            keys = list(tags.keys())
        except Exception:
            keys = []
        for k in keys:
            ks = str(k)
            try:
                v = tags[k]
            except Exception:
                continue
            if ks.startswith("APIC"):                         # ID3
                pics.append((getattr(v, "type", 0), v.data))
            elif ks == "covr":                                # MP4
                for c in (v or []):
                    pics.append((3, bytes(c)))
            elif ks.lower() == "metadata_block_picture":      # Ogg / Opus
                try:
                    import base64
                    from mutagen.flac import Picture
                    for b in (v if isinstance(v, list) else [v]):
                        p = Picture(base64.b64decode(b))
                        pics.append((p.type, p.data))
                except Exception:
                    pass
    pics = [(t, d) for t, d in pics if d]
    if not pics:
        return None
    pics.sort(key=lambda p: 0 if p[0] == 3 else 1)            # 3 = front cover
    data = bytes(pics[0][1])
    return data, _sniff_mime(data)


def folder_image(abs_dir: str, any_image: bool = False):
    """! @brief The image that stands for a folder: cover.jpg / folder.png / front.* ...
    first; with `any_image` (an album or artist folder in a music section, laid out
    like Plex) any image in it, by name. None when there is none."""
    try:
        entries = sorted(os.listdir(abs_dir))
    except OSError:
        return None
    names = {n.lower(): n for n in entries}
    for stem in _FOLDER_COVERS:
        for ext in _IMG_EXTS:
            n = names.get(stem + ext)
            if n:
                return os.path.join(abs_dir, n)
    if any_image:
        for n in entries:
            if not n.startswith(".") and os.path.splitext(n)[1].lower() in _IMG_EXTS:
                return os.path.join(abs_dir, n)
    return None


def folder_cover_path(abs_path: str, any_image: bool = False):
    """! @brief A cover image beside the track (cover.jpg, folder.png, ...), or None."""
    return folder_image(os.path.dirname(abs_path), any_image)


def folder_description(abs_dir: str, book_exts):
    """! @brief The description file of an album / artist folder: the first book-kind
    file in it by name (`book_exts`: lower-case extensions with the dot, the book
    media kind's), or None."""
    try:
        entries = sorted(os.listdir(abs_dir))
    except OSError:
        return None
    exts = set(book_exts or ())
    for n in entries:
        if not n.startswith(".") and os.path.splitext(n)[1].lower() in exts:
            return os.path.join(abs_dir, n)
    return None


def read_description(abs_path: str, limit: int = 4000) -> str | None:
    """! @brief The text of a plain-text description (.txt / .md / .nfo), up to `limit`
    characters; None for any other format (the file is still linked by path)."""
    if os.path.splitext(abs_path)[1].lower() not in _TEXT_EXTS:
        return None
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit).strip()
    except OSError:
        return None


def read_cover(abs_path: str, any_image: bool = False):
    """! @brief (bytes, mime) of a track's cover: the embedded art, else the folder's
    cover image (any image in the folder with `any_image`); None when it has
    neither. Never raises."""
    try:
        emb = _embedded_cover(abs_path) if HAVE_MUTAGEN else None
        if emb:
            return emb
        fp = folder_cover_path(abs_path, any_image)
        if fp:
            with open(fp, "rb") as f:
                data = f.read()
            return data, _sniff_mime(data)
    except Exception:
        pass
    return None

# -- embedding ------------------------------------------------------------------
def _pack_emb(vec: np.ndarray) -> bytes:
    v = np.asarray(vec, dtype=np.float32).ravel()
    return struct.pack("<I", v.size) + v.tobytes()

def unpack_emb(blob) -> np.ndarray | None:
    if not blob:
        return None
    try:
        n = struct.unpack("<I", blob[:4])[0]
        return np.frombuffer(blob[4:4 + n * 4], dtype=np.float32).copy()
    except Exception:
        return None

def compute_embedding(abs_path: str, max_seconds: float = 90.0) -> np.ndarray | None:
    """! @brief Deterministic offline audio fingerprint suitable for similarity/clustering.

    Loads up to `max_seconds` (mono, 22.05 kHz), takes summary statistics of
    timbral + harmonic + rhythmic features, and concatenates them into a single
    fixed-length vector. No network, no model weights.
    """
    if not HAVE_LIBROSA:
        return None
    try:
        y, sr = librosa.load(abs_path, sr=22050, mono=True, duration=max_seconds)
        if y is None or y.size < sr:          # < 1s of audio -> skip
            return None

        mfcc      = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=20)
        chroma    = librosa.feature.chroma_stft(y=y, sr=sr)
        contrast  = librosa.feature.spectral_contrast(y=y, sr=sr)
        zcr       = librosa.feature.zero_crossing_rate(y)
        rms       = librosa.feature.rms(y=y)
        tempo, _  = librosa.beat.beat_track(y=y, sr=sr)

        parts = [
            mfcc.mean(axis=1), mfcc.std(axis=1),   # 40
            chroma.mean(axis=1),                   # 12
            contrast.mean(axis=1),                 # 7
            np.array([float(np.atleast_1d(tempo)[0]) / 250.0]),  # 1 (normalised)
            np.array([float(zcr.mean())]),         # 1
            np.array([float(rms.mean())]),         # 1
        ]
        vec = np.concatenate(parts).astype(np.float32)
        # guard against NaN/inf from silent or corrupt files
        vec = np.nan_to_num(vec, nan=0.0, posinf=0.0, neginf=0.0)
        return vec
    except Exception:
        return None

def normalize_matrix(M: np.ndarray, zscore: bool = True) -> np.ndarray:
    """! @brief Rows L2-normalised -> cosine == dot product. zscore=True first z-scores
    each column: right for the hand-crafted librosa fingerprint (features on
    wildly different scales), wrong for a model space (CLAP, MuQ) where text
    queries must stay comparable to the stored vectors."""
    M = np.asarray(M, dtype=np.float32)
    if zscore:
        mu = M.mean(axis=0, keepdims=True)
        sd = M.std(axis=0, keepdims=True) + 1e-6
        M = (M - mu) / sd
    n = np.linalg.norm(M, axis=1, keepdims=True) + 1e-9
    return M / n

def is_fingerprint_space(space) -> bool:
    return str(space or "") == EMB_SIG

# -- clustering ------------------------------------------------------------------
def _kmeans_np(X, k, iters=25, seed=0):
    """! @brief Plain numpy k-means (cosine rows): the no-sklearn fallback."""
    rng = np.random.default_rng(seed)
    C = X[rng.choice(len(X), size=k, replace=False)].copy()
    labels = np.zeros(len(X), dtype=int)
    for _ in range(iters):
        new = np.argmax(X @ C.T, axis=1)
        if np.array_equal(new, labels) and _:
            break
        labels = new
        for c in range(k):
            m = labels == c
            if m.any():
                v = X[m].mean(axis=0); C[c] = v / (np.linalg.norm(v) + 1e-9)
    return labels

def kmeans_labels(X, k, seed=0):
    if HAVE_SKLEARN:
        return KMeans(n_clusters=k, n_init=4, random_state=seed).fit(X).labels_
    return _kmeans_np(X, k, seed=seed)

def cluster_embeddings(paths, embs, k=None, zscore=True):
    """! @brief KMeans over a list of embeddings. Returns {rel_path: cluster_id} and k."""
    X = normalize_matrix(np.vstack(embs), zscore=zscore)
    n = len(paths)
    if k is None:
        k = max(2, min(40, int(round(np.sqrt(n / 2)))))
    k = min(k, n)
    labels = kmeans_labels(X, k)
    return {p: int(c) for p, c in zip(paths, labels)}, k

# -- similarity / shuffle --------------------------------------------------------
def shuffle_by(seed_vecs, all_paths, all_embs, temperature=0.25, limit=500, zscore=True):
    """! @brief Order tracks by similarity to the seed centroid, with controlled noise.

    seed_vecs : list of embeddings defining the seed (one song, or every song by
                an artist). Their mean is the centroid.
    Returns rel_paths ordered most->least similar, jittered so repeated presses
    give a fresh-but-coherent playlist.
    """
    if not all_embs:
        return []
    M = normalize_matrix(np.vstack(all_embs), zscore=zscore)
    centroid = normalize_matrix(np.vstack(seed_vecs), zscore=zscore).mean(axis=0)
    centroid = centroid / (np.linalg.norm(centroid) + 1e-9)
    sims = M @ centroid                      # cosine, already row-normalised
    # add gaussian jitter scaled by temperature so the order is a playlist
    noise = np.random.normal(0, temperature, size=sims.shape)
    score = sims + noise
    order = np.argsort(-score)
    return [all_paths[i] for i in order[:limit]]
# -- radio: one route through the whole library ---------------------------------
def _nn_tour(X, start=0):
    """! @brief Greedy nearest-unvisited tour over rows of X (cosine). O(n^2) time, O(n)
    memory. Returns index order."""
    n = len(X)
    visited = np.zeros(n, dtype=bool)
    order = [start]; visited[start] = True
    cur = start
    for _ in range(n - 1):
        sims = X @ X[cur]
        sims[visited] = -np.inf
        cur = int(np.argmax(sims)); visited[cur] = True
        order.append(cur)
    return order

def _two_opt(X, order, rounds=3):
    """! @brief Cheap 2-opt on a short tour (cluster centroids): uncross edges."""
    D = 1.0 - X @ X.T
    o = list(order); n = len(o)
    if n < 4:
        return o
    for _ in range(rounds):
        improved = False
        for i in range(1, n - 2):
            for j in range(i + 1, n - 1):
                a, b, c, d = o[i - 1], o[i], o[j], o[j + 1]
                if D[a, c] + D[b, d] < D[a, b] + D[c, d] - 1e-9:
                    o[i:j + 1] = o[i:j + 1][::-1]; improved = True
        if not improved:
            break
    return o

def route_playlist(paths, embs, start_vec=None, zscore=True, seed=None):
    """! @brief Every track once, ordered as one smooth walk through embedding space:
    rock -> blues -> jazz -> classical, each step to a near neighbour.

    Cluster (k ~ sqrt(n/2)), tour the centroids (greedy + 2-opt), then walk each
    cluster greedily from the member nearest the previous cluster's last
    track. `start_vec` picks the first track (the one nearest it), so a new
    round can begin where the previous one ended; otherwise a random track.
    """
    n = len(paths)
    if n == 0:
        return []
    if n <= 3:
        return list(paths)
    X = normalize_matrix(np.vstack(embs), zscore=zscore)
    rng = np.random.default_rng(seed)
    if start_vec is not None and np.asarray(start_vec).size == X.shape[1]:
        q = np.asarray(start_vec, np.float32); q = q / (np.linalg.norm(q) + 1e-9)
        first = int(np.argmax(X @ q))
    else:
        first = int(rng.integers(n))
    k = max(2, min(60, int(round(np.sqrt(n / 2)))))
    labels = kmeans_labels(X, min(k, n), seed=int(rng.integers(1 << 30)))
    members = {c: np.flatnonzero(labels == c) for c in np.unique(labels)}
    cids = sorted(members)
    C = np.vstack([X[members[c]].mean(axis=0) for c in cids])
    C = C / (np.linalg.norm(C, axis=1, keepdims=True) + 1e-9)
    c_start = cids.index(int(labels[first]))
    c_order = _two_opt(C, _nn_tour(C, start=c_start))
    # rotate so the tour starts at the cluster holding `first`
    c_order = c_order[c_order.index(c_start):] + c_order[:c_order.index(c_start)]
    out = []
    prev = X[first]; entry = first
    for ci in c_order:
        idx = members[cids[ci]]
        Xi = X[idx]
        if entry is not None and entry in set(idx.tolist()):
            s0 = int(np.flatnonzero(idx == entry)[0])
        else:
            s0 = int(np.argmax(Xi @ prev))
        local = _nn_tour(Xi, start=s0) if len(idx) > 1 else [0]
        out.extend(int(idx[i]) for i in local)
        prev = X[out[-1]]; entry = None
    return [paths[i] for i in out]