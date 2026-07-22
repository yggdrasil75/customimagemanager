"""
book_index.py — books & comics side of the media manager.
=========================================================

Self-contained module in the same shape as music_index.py, so it bolts onto
manager.py without touching the image pipeline. manager.py owns the DB handle,
MEDIA_DIR and the OAI embedding client; this module owns everything that knows
what a *book* is.

WHY BOOKS ARE HARDER THAN AUDIO
───────────────────────────────
For music, ".mp3 means music" is true. For books it is emphatically not:

    .txt   is a book  OR  the tag sidecar this app writes next to every asset
    .htm(l) is a book OR  a saved webpage OR one chapter inside an unpacked epub
    .pdb   is a book  OR  a generic Palm database (contacts, memos, anything)
    .opf   is a book's *manifest*, not a book — the folder around it is the book
    .doc   is a book  OR  any other OLE2 compound document
    .pkg   is a book  OR  a macOS installer
    .cbz   is a comic OR  (very often) a RAR that someone renamed
    .pdf   is a book  OR  a comic OR a scanned receipt

So classification here is THREE layers, in this order:

  1. `ext_candidate()`  — is this extension even in the running? (cheap)
  2. `sniff()`          — what do the first bytes actually say? (cheap, decisive
                          for ~everything with a magic number)
  3. `classify()`       — context: what else is in this directory? Is there a
                          sibling asset with the same basename (→ sidecar)? A
                          sibling `mimetype`/`.opf`/`toc.ncx` (→ we're INSIDE an
                          unpacked book, so the folder is the unit, not us)? A
                          `foo_files/` dir (→ saved webpage)?

Anything the three layers cannot settle is NOT guessed. It lands in the
`book_triage` table with the reason, and the UI asks the human once. With
thousands of ao3 dumps and Kindle exports, a silent 2% misfile rate is worse
than a triage queue you can clear with two clicks.

READER MODEL
────────────
Two render modes, because there are genuinely two kinds of book:

  • 'paged' — PDF and every cb* comic archive. The page IS an image. The reader
    asks for `/api/books/page/<n>` and gets a JPEG. This reuses the existing
    comic viewer's mental model.

  • 'flow'  — epub, txt, html, fb2, mobi/azw, docx, rtf, lit, chm, pdb… The page
    is a reader-side concept. We extract to sanitized HTML once, cache it as
    chapters, and the reader does its own pagination/columns/font sizing.

EXTRACTION BACKENDS
───────────────────
Native Python first (ebooklib / pymupdf / rarfile / py7zr / python-docx /
striprtf / stdlib zipfile+tarfile), then Calibre's `ebook-convert` CLI as the
universal fallback for the long tail (.lit, .chm, .ceb, .kfx, .pdb, .azw*).
Nothing here is a hard dependency: every backend is imported lazily and a
missing one degrades to `status='needs_backend'` with a message naming what to
install, rather than an exception.

EMBEDDING SEARCH
────────────────
A book is far too long for one vector. We chunk the extracted text (~1200 chars,
200 overlap), embed each chunk with the same OAI embedding model the images use,
and store them in `book_chunks`. Search then returns *passages*, and the book
score is the max over its chunks — so "the bit where they argue on the bridge"
finds the book AND the page.
"""
from __future__ import annotations

import io
import os
import re
import json
import time
import struct
import zipfile
import tarfile
import hashlib
import subprocess
import threading
import html as _html
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

import numpy as np


# ══════════════════════════════════════════════════════════════════════════════
# 1. EXTENSIONS
# ══════════════════════════════════════════════════════════════════════════════

# Comic archives. Container-per-extension; the real container is sniffed because
# .cbz-that-is-actually-a-rar is extremely common in scene releases.
COMIC_ARCHIVE_EXTS = {'.cbz', '.cbr', '.cb7', '.cbt', '.cba'}

# Formats whose extension is unambiguous enough to accept on sight (still
# sniffed, but a sniff failure doesn't disqualify them).
UNAMBIGUOUS_BOOK_EXTS = {
    '.epub', '.mobi', '.azw', '.azw3', '.kf8', '.kfx', '.lit', '.fb2',
    '.lrf', '.lrx', '.chm', '.ceb', '.docx', '.rtf',
} | COMIC_ARCHIVE_EXTS

# Formats that are books *sometimes*. Each has a dedicated rule in classify().
AMBIGUOUS_BOOK_EXTS = {
    '.pdf',      # book | comic | scanned junk    → accepted, kind decided later
    '.txt',      # book | this app's tag sidecar
    '.htm', '.html',  # book | webpage | epub innards
    '.doc',      # book | any OLE2 compound file
    '.pdb',      # book | any Palm database
    '.pkg',      # book | macOS installer
    '.opf',      # a book's manifest — the FOLDER is the book
}

BOOK_EXTS = UNAMBIGUOUS_BOOK_EXTS | AMBIGUOUS_BOOK_EXTS

# Extensions this app already writes as sidecars next to library assets. A .txt
# whose basename matches one of these assets is a tag sidecar, never a book.
_ASSET_EXTS_FOR_SIDECAR = {
    '.jxl', '.mp4', '.mkv', '.webm', '.mov', '.avi', '.m4v', '.mpg', '.mpeg',
    '.wmv', '.flv', '.ts', '.ogv', '.mp3', '.flac', '.aac', '.ogg', '.oga',
    '.opus', '.wav', '.wma', '.aiff', '.aif',
}

# Filenames that mark the directory we're standing in as the *insides* of an
# unpacked book. Any candidate found alongside these is a component, not a book.
_UNPACKED_BOOK_MARKERS = {
    'mimetype', 'container.xml', 'toc.ncx', 'content.opf', 'package.opf',
}

# Page-image extensions inside a comic archive.
_PAGE_IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.jxl', '.avif'}

# Which reader a format uses.
PAGED_FORMATS = {'pdf', 'cbz', 'cbr', 'cb7', 'cbt', 'cba'}

# Chunking for embedding search.
CHUNK_CHARS = 1200
CHUNK_OVERLAP = 200
EMB_SIG_PREFIX = "bookchunk-v1"

_lock = threading.Lock()


def _ext(path: str) -> str:
    return os.path.splitext(path)[1].lower()


def ext_candidate(path: str) -> bool:
    """Layer 1: is this extension even in the running for being a book?"""
    return _ext(path) in BOOK_EXTS


# ══════════════════════════════════════════════════════════════════════════════
# 2. CONTENT SNIFFING
# ══════════════════════════════════════════════════════════════════════════════

# Palm PDB type+creator codes at offset 60..68. This is the ONLY thing that
# separates "a book" from "someone's 2003 address book" for .pdb/.prc/.pkg.
_PALM_BOOK_TYPES = {
    b'BOOKMOBI': 'mobi',      # Mobipocket / Kindle .mobi .azw .prc
    b'TEXtREAd': 'palmdoc',   # PalmDOC / AportisDoc
    b'PNRdPPrs': 'ereader',   # eReader (Peanut Press)
    b'DataPlkr': 'plucker',   # Plucker
    b'BVokBDIC': 'bdic',
    b'zTXTGPlm': 'ztxt',      # Weasel zTXT
}


def _read_head(path: str, n: int = 4096) -> bytes:
    try:
        with open(path, 'rb') as f:
            return f.read(n)
    except Exception:
        return b''


def _zip_names(path: str, limit: int = 400) -> list[str]:
    try:
        with zipfile.ZipFile(path) as z:
            return z.namelist()[:limit]
    except Exception:
        return []


def sniff(path: str) -> str | None:
    """Layer 2: what format do the BYTES say this is?

    Returns a canonical format id ('epub', 'pdf', 'mobi', 'cbz', 'zip', 'rar',
    'ole2', 'text', 'html', …) or None if nothing matched. This never trusts the
    filename, which is the whole point — a .cbz holding a RAR reports 'rar', and
    a .pdb holding contacts reports 'palm-other' so classify() can reject it.
    """
    head = _read_head(path, 8192)
    if len(head) < 8:
        return None

    # ── magic numbers, most specific first ────────────────────────────────────
    if head[:5] == b'%PDF-':
        return 'pdf'
    if head[:4] == b'ITSF':
        return 'chm'
    if head[:8] == b'ITOLITLS':
        return 'lit'
    if head[:4] in (b'Rar!', b'\x52\x61\x72\x21'):
        return 'rar'
    if head[:6] == b'7z\xbc\xaf\x27\x1c':
        return '7z'
    if head[:7] == b'**ACE**' or head[7:14] == b'**ACE**':
        return 'ace'
    if head[:4] == b'{\\rt':
        return 'rtf'
    if head[:8] == b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1':
        return 'ole2'                      # .doc, and a hundred other things
    if head[:4] == b'CONT' or head[:8] == b'\xeaDRMION\xee':
        return 'kfx'
    if head[:4] == b'LRX\x00' or head[:8].startswith(b'L\x00R\x00F'):
        return 'lrf'
    if head[:8] == b'\x4c\x00\x00\x00\x42\x4f\x4f\x4b':   # BBeB "LRF"
        return 'lrf'
    if head[:4] == b'CEBX' or head[:4] == b'\x43\x45\x42\x58':
        return 'ceb'

    # tar (comic .cbt) — magic lives at offset 257
    if len(head) > 262 and head[257:262] == b'ustar':
        return 'tar'

    # ── Palm database family (.pdb .prc .mobi .azw .pkg) ──────────────────────
    # 32-byte name, then attrs/version/dates…, type at 60, creator at 64.
    if len(head) >= 68:
        tc = head[60:68]
        if tc in _PALM_BOOK_TYPES:
            return _PALM_BOOK_TYPES[tc]
        # Looks structurally like a PDB (printable type/creator) but isn't a
        # book type → say so explicitly so classify() can reject with a reason.
        if all(32 <= b < 127 for b in tc) and head[0] not in (0, 32):
            return 'palm-other'

    # ── ZIP-based: epub vs cbz vs docx vs plain zip ───────────────────────────
    if head[:2] == b'PK':
        try:
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
                nameset = set(names)
                if 'mimetype' in nameset:
                    try:
                        mt = z.read('mimetype').strip()
                        if mt == b'application/epub+zip':
                            return 'epub'
                    except Exception:
                        pass
                if 'word/document.xml' in nameset:
                    return 'docx'
                if any(n.lower().endswith('.opf') for n in names):
                    return 'epub'            # epub missing its mimetype entry
                imgs = sum(1 for n in names
                           if _ext(n) in _PAGE_IMAGE_EXTS and not n.endswith('/'))
                real = sum(1 for n in names if not n.endswith('/'))
                if real and imgs / real >= 0.8 and imgs >= 3:
                    return 'cbz'
                return 'zip'
        except Exception:
            return 'zip'

    # ── XML-ish: fb2 vs xhtml vs opf ──────────────────────────────────────────
    probe = head[:4096].lstrip(b'\xef\xbb\xbf').lstrip()
    low = probe[:2048].lower()
    if low.startswith(b'<?xml') or low.startswith(b'<'):
        if b'<fictionbook' in low:
            return 'fb2'
        if b'<package' in low and b'opf' in low:
            return 'opf'
        if b'<html' in low or b'<!doctype html' in low or b'<head' in low:
            return 'html'
        return 'xml'

    # ── plain text (heuristic: mostly printable, decodes as utf-8/latin-1) ────
    if _looks_like_text(head):
        return 'text'
    return None


def _looks_like_text(buf: bytes) -> bool:
    if not buf:
        return False
    if b'\x00' in buf[:1024]:
        return False
    try:
        buf.decode('utf-8')
        return True
    except UnicodeDecodeError:
        pass
    printable = sum(1 for b in buf if 9 <= b <= 13 or 32 <= b < 127 or b >= 160)
    return printable / len(buf) > 0.90


# ══════════════════════════════════════════════════════════════════════════════
# 3. CONTEXTUAL CLASSIFICATION
# ══════════════════════════════════════════════════════════════════════════════

class Verdict:
    """Result of classify(). `status` is one of:

      'book'     — index it. `fmt` is the canonical format, `kind` book|comic.
      'sidecar'  — this app's own metadata file. Skip silently.
      'part'     — a component of a book that lives elsewhere (a chapter inside
                   an unpacked epub, a page inside a comic folder). Skip.
      'skip'     — confidently not a book (Palm address book, installer, …).
      'triage'   — could be a book; we won't guess. Ask the human.
    """
    __slots__ = ('status', 'fmt', 'kind', 'reason', 'confidence')

    def __init__(self, status, fmt=None, kind=None, reason='', confidence=1.0):
        self.status = status
        self.fmt = fmt
        self.kind = kind
        self.reason = reason
        self.confidence = confidence

    def __repr__(self):
        return f"<Verdict {self.status} fmt={self.fmt} kind={self.kind} {self.reason!r}>"

    def as_dict(self):
        return {'status': self.status, 'fmt': self.fmt, 'kind': self.kind,
                'reason': self.reason, 'confidence': self.confidence}


def _dir_context(abs_path: str) -> dict:
    """Everything classify() needs to know about the file's neighbourhood.

    Computed once per directory by the caller when scanning in bulk (see
    `walk_candidates`), because os.listdir per file on a 50k-file library is the
    difference between a 20-second scan and a 10-minute one.
    """
    d = os.path.dirname(abs_path)
    try:
        entries = os.listdir(d)
    except OSError:
        entries = []
    files, dirs = set(), set()
    for e in entries:
        (dirs if os.path.isdir(os.path.join(d, e)) else files).add(e)
    return _context_from_listing(files, dirs)


def _context_from_listing(files: set, dirs: set) -> dict:
    lower_files = {f.lower() for f in files}
    return {
        'files': files,
        'lower_files': lower_files,
        'dirs': dirs,
        'lower_dirs': {d.lower() for d in dirs},
        # basenames of real library assets, for the .txt sidecar test
        'asset_stems': {os.path.splitext(f)[0]
                        for f in files if _ext(f) in _ASSET_EXTS_FOR_SIDECAR},
        'unpacked_marker': bool(lower_files & _UNPACKED_BOOK_MARKERS),
        'page_images': sum(1 for f in files if _ext(f) in _PAGE_IMAGE_EXTS),
    }


def classify(abs_path: str, ctx: dict | None = None) -> Verdict:
    """Layer 3. The single entry point the indexer calls per candidate file."""
    ext = _ext(abs_path)
    name = os.path.basename(abs_path)
    stem = os.path.splitext(name)[0]

    if ext not in BOOK_EXTS:
        return Verdict('skip', reason='extension not a book candidate')

    if ctx is None:
        ctx = _dir_context(abs_path)

    # ── Rule 0: are we standing inside an unpacked book? ──────────────────────
    # An extracted epub is a directory of .html + .opf + .ncx. Every one of those
    # .html files is a *chapter*, not a book. The .opf is what represents it.
    if ctx['unpacked_marker'] and ext in ('.htm', '.html', '.txt', '.xml'):
        return Verdict('part', reason='component of an unpacked book '
                                      '(sibling mimetype/opf/toc.ncx)')

    fmt = sniff(abs_path)

    # ── Rule 1: .txt — book or this app's tag sidecar? ────────────────────────
    if ext == '.txt':
        return _classify_txt(abs_path, stem, ctx)

    # ── Rule 2: .htm(l) — book, saved webpage, or epub innards? ───────────────
    if ext in ('.htm', '.html'):
        return _classify_html(abs_path, stem, ctx, fmt)

    # ── Rule 3: .opf — the manifest represents its folder ─────────────────────
    if ext == '.opf':
        # Only treat it as the book if the folder actually holds content.
        if ctx['unpacked_marker'] or any(_ext(f) in ('.htm', '.html', '.xhtml')
                                         for f in ctx['files']):
            return Verdict('book', 'opf-folder', 'book',
                           'manifest of an unpacked book; folder is the unit')
        return Verdict('triage', 'opf', 'book',
                       'stray .opf with no content beside it')

    # ── Rule 4: Palm-family containers (.pdb, .pkg) ───────────────────────────
    if ext in ('.pdb', '.pkg'):
        if fmt in ('mobi', 'palmdoc', 'ereader', 'plucker', 'ztxt'):
            return Verdict('book', fmt, 'book', f'Palm type code says {fmt}')
        if fmt == 'palm-other':
            return Verdict('skip', reason='Palm database, but not a book type')
        if fmt == 'bdic':
            return Verdict('skip', reason='Palm dictionary, not a book')
        return Verdict('triage', fmt, 'book',
                       f'{ext} with unrecognised content ({fmt or "unknown"})')

    # ── Rule 5: .doc — OLE2 could be anything ─────────────────────────────────
    if ext == '.doc':
        if fmt == 'ole2':
            if _ole2_has_word_stream(abs_path):
                return Verdict('book', 'doc', 'book', 'OLE2 with WordDocument stream')
            return Verdict('triage', 'ole2', 'book',
                           'OLE2 compound file with no WordDocument stream')
        if fmt == 'rtf':
            return Verdict('book', 'rtf', 'book', '.doc that is really RTF')
        if fmt in ('text', 'html'):
            return Verdict('book', fmt, 'book', f'.doc that is really {fmt}')
        return Verdict('triage', fmt, 'book', 'unrecognised .doc content')

    # ── Rule 6: comic archives — trust the CONTENT for the container ──────────
    if ext in COMIC_ARCHIVE_EXTS:
        real = {'zip': 'cbz', 'cbz': 'cbz', 'rar': 'cbr', '7z': 'cb7',
                'tar': 'cbt', 'ace': 'cba'}.get(fmt or '')
        if real:
            declared = ext.lstrip('.')
            note = '' if real == declared else f' (declared .{declared}, really {fmt})'
            return Verdict('book', real, 'comic', f'comic archive{note}')
        if fmt == 'epub':
            return Verdict('book', 'epub', 'comic', 'epub-packaged comic')
        return Verdict('triage', fmt, 'comic',
                       f'comic extension but content sniffs as {fmt or "unknown"}')

    # ── Rule 7: PDF — book or comic? ──────────────────────────────────────────
    if ext == '.pdf':
        if fmt != 'pdf':
            return Verdict('triage', fmt, 'book', 'named .pdf but not a PDF')
        return Verdict('book', 'pdf', 'book', 'PDF')   # kind refined at index time

    # ── Rule 8: everything left is an unambiguous extension ───────────────────
    canonical = {
        '.epub': 'epub', '.mobi': 'mobi', '.azw': 'mobi', '.azw3': 'azw3',
        '.kf8': 'azw3', '.kfx': 'kfx', '.lit': 'lit', '.fb2': 'fb2',
        '.lrf': 'lrf', '.lrx': 'lrf', '.chm': 'chm', '.ceb': 'ceb',
        '.docx': 'docx', '.rtf': 'rtf',
    }.get(ext)
    if canonical:
        # Sniff disagreeing is worth a note but not a rejection — several of
        # these (.lrx, .ceb, .kfx) have poorly documented magic.
        if fmt and fmt not in (canonical, 'zip', 'xml', None):
            if fmt in ('rar', '7z', 'ole2') and canonical in ('epub', 'docx'):
                return Verdict('triage', fmt, 'book',
                               f'.{ext.lstrip(".")} but content is {fmt}')
        return Verdict('book', canonical, 'book', f'{canonical} by extension')

    return Verdict('triage', fmt, 'book', 'unclassified')


def _classify_txt(abs_path: str, stem: str, ctx: dict) -> Verdict:
    """.txt is the nastiest case: this app writes tag sidecars as .txt.

    Tests, in order of decisiveness:
      1. A sibling library asset with the same stem  → sidecar. Definitive.
      2. Tiny file                                   → sidecar-shaped.
      3. Content shape: one line of comma-separated short tokens with no
         sentence punctuation is a tag list, not prose.
      4. Otherwise: prose. It's a book.
    """
    if stem in ctx['asset_stems']:
        return Verdict('sidecar', reason='tag sidecar for a library asset '
                                         'with the same basename')
    try:
        size = os.path.getsize(abs_path)
    except OSError:
        return Verdict('skip', reason='unreadable')

    if size < 512:
        return Verdict('sidecar', reason=f'{size} B — too small to be a book')

    head = _read_head(abs_path, 8192)
    if not _looks_like_text(head):
        return Verdict('skip', reason='.txt that is not text')

    try:
        text = head.decode('utf-8', 'replace')
    except Exception:
        text = ''

    stripped = text.strip()
    first_block = stripped.split('\n\n', 1)[0]
    # A tag sidecar: few/no newlines, comma-heavy, no sentence enders.
    if size < 4096:
        commas = first_block.count(',')
        newlines = stripped.count('\n')
        sentence_enders = len(re.findall(r'[.!?]["\')\]]?\s', stripped))
        if commas >= 3 and newlines <= 2 and sentence_enders == 0:
            return Verdict('sidecar',
                           reason='single comma-separated line with no prose')
        return Verdict('triage', 'text', 'book',
                       f'{size} B .txt — short enough to be either')

    # Real prose: sentences, paragraphs, plausible word length.
    words = re.findall(r"[A-Za-z']+", text)
    if len(words) < 50:
        return Verdict('triage', 'text', 'book', 'too few words to be sure')
    return Verdict('book', 'text', 'book', f'{size} B of prose')


def _classify_html(abs_path: str, stem: str, ctx: dict, fmt: str | None) -> Verdict:
    """.htm(l): standalone story (very common for ao3/ffn downloads), a saved
    webpage, or a chapter inside an unpacked epub."""
    # Saved-webpage marker: Chrome/IE write `Foo.html` + `Foo_files/`.
    for suffix in ('_files', '.files', '_arquivos', '-Dateien'):
        if (stem + suffix).lower() in ctx['lower_dirs']:
            return Verdict('skip', reason=f'saved webpage (sibling {stem}{suffix}/)')

    if stem in ctx['asset_stems']:
        return Verdict('part', reason='html beside a same-named library asset')

    try:
        size = os.path.getsize(abs_path)
    except OSError:
        return Verdict('skip', reason='unreadable')

    head = _read_head(abs_path, 65536)
    low = head.lower()

    # Positive markers for the ebook-ish HTML people actually archive.
    if b'archiveofourown.org' in low or b'fanfiction.net' in low:
        return Verdict('book', 'html', 'book', 'fanfiction archive export')
    if b'<meta name="generator" content="calibre' in low:
        return Verdict('book', 'html', 'book', 'calibre-generated HTML')
    if b'epub:type' in low or b'application/xhtml+xml' in low and ctx['unpacked_marker']:
        return Verdict('part', reason='xhtml chapter of an unpacked epub')

    # Navigation-heavy HTML with little text is a webpage, not a book.
    text_len = len(_strip_tags(head.decode('utf-8', 'replace')))
    link_count = low.count(b'<a ')
    if size < 8192 and text_len < 1500:
        return Verdict('skip', reason='small HTML with little text — a webpage')
    if link_count > 60 and text_len < 4000:
        return Verdict('skip', reason='link-dense HTML — an index page')
    if text_len > 6000:
        return Verdict('book', 'html', 'book', f'{text_len} chars of body text')
    return Verdict('triage', 'html', 'book',
                   f'{text_len} chars of text, {link_count} links — ambiguous')


def _ole2_has_word_stream(path: str) -> bool:
    """Cheap check for a WordDocument stream in an OLE2 compound file.

    A full CFB directory walk needs a parser; the stream NAME is stored as
    UTF-16LE in the directory sector, so scanning the first 64 KB for the
    encoded literal is both correct in practice and effectively free.
    """
    head = _read_head(path, 65536)
    return b'W\x00o\x00r\x00d\x00D\x00o\x00c\x00u\x00m\x00e\x00n\x00t\x00' in head


# ══════════════════════════════════════════════════════════════════════════════
# 4. SCHEMA
# ══════════════════════════════════════════════════════════════════════════════

def ensure_tables(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS books (
            rel_path     TEXT PRIMARY KEY,
            mtime        REAL,
            size         INTEGER,
            fmt          TEXT,            -- epub | pdf | cbz | mobi | text | …
            kind         TEXT,            -- 'book' | 'comic'
            reader       TEXT,            -- 'flow' | 'paged'
            -- editable metadata --
            title        TEXT DEFAULT '',
            sort_title   TEXT DEFAULT '',
            authors      TEXT DEFAULT '[]',   -- JSON list
            series       TEXT DEFAULT '',
            series_index REAL,
            publisher    TEXT DEFAULT '',
            published    TEXT DEFAULT '',
            language     TEXT DEFAULT '',
            isbn         TEXT DEFAULT '',
            identifiers  TEXT DEFAULT '{}',   -- JSON dict (asin, ao3, goodreads…)
            description  TEXT DEFAULT '',
            subjects     TEXT DEFAULT '[]',   -- JSON list (publisher's own)
            tags         TEXT DEFAULT '[]',   -- JSON list (user's)
            rating       INTEGER DEFAULT 0,
            -- derived --
            page_count   INTEGER,
            word_count   INTEGER,
            cover        TEXT DEFAULT '',     -- rel path under .bookcache/
            text_status  TEXT DEFAULT 'pending',
                         -- pending | ok | needs_backend | failed | unsupported
            text_error   TEXT DEFAULT '',
            emb_status   TEXT DEFAULT 'pending',
            sha256       TEXT,                -- content hash, filled lazily
                         -- Not computed at index time: hashing 3000 books would
                         -- add minutes to a scan for a check most people never
                         -- trigger. Filled on demand by book_routes.sha_exists.
            source       TEXT DEFAULT '',     -- ao3 | kindle | gutenberg | …
            added        REAL,
            indexed      REAL
        );
        CREATE INDEX IF NOT EXISTS idx_books_sha ON books(sha256);
        CREATE INDEX IF NOT EXISTS idx_books_series ON books(series);
        CREATE INDEX IF NOT EXISTS idx_books_kind   ON books(kind);
        CREATE INDEX IF NOT EXISTS idx_books_title  ON books(sort_title);

        -- One row per author per book, so "browse by author" is an index scan
        -- rather than a JSON LIKE over the whole table.
        CREATE TABLE IF NOT EXISTS book_authors (
            rel_path TEXT,
            author   TEXT,
            PRIMARY KEY (rel_path, author)
        );
        CREATE INDEX IF NOT EXISTS idx_bauthor ON book_authors(author);

        -- Extracted, sanitized reading text. One row per chapter/section for
        -- 'flow' books; 'paged' books render on demand and store nothing here.
        CREATE TABLE IF NOT EXISTS book_sections (
            rel_path TEXT,
            idx      INTEGER,
            title    TEXT DEFAULT '',
            html     TEXT,
            chars    INTEGER,
            PRIMARY KEY (rel_path, idx)
        );

        -- Passage-level embeddings. Search returns passages; a book's score is
        -- the max over its chunks.
        CREATE TABLE IF NOT EXISTS book_chunks (
            rel_path TEXT,
            idx      INTEGER,
            section  INTEGER,
            offset   INTEGER,
            text     TEXT,
            emb      BLOB,
            emb_sig  TEXT,
            PRIMARY KEY (rel_path, idx)
        );
        CREATE INDEX IF NOT EXISTS idx_bchunk_sig ON book_chunks(emb_sig);

        -- Per-user reading position. `locator` is section+char for flow books,
        -- page number for paged ones.
        CREATE TABLE IF NOT EXISTS book_progress (
            rel_path TEXT,
            user     TEXT DEFAULT '',
            locator  TEXT DEFAULT '',
            percent  REAL DEFAULT 0,
            updated  REAL,
            PRIMARY KEY (rel_path, user)
        );

        CREATE TABLE IF NOT EXISTS book_bookmarks (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            rel_path TEXT,
            user     TEXT DEFAULT '',
            locator  TEXT DEFAULT '',
            label    TEXT DEFAULT '',
            note     TEXT DEFAULT '',
            created  REAL
        );
        CREATE INDEX IF NOT EXISTS idx_bmark ON book_bookmarks(rel_path);

        -- The triage queue: files we refused to guess about. `decision` is
        -- NULL until a human answers; then 'book' or 'not_book'.
        CREATE TABLE IF NOT EXISTS book_triage (
            rel_path TEXT PRIMARY KEY,
            ext      TEXT,
            sniffed  TEXT,
            reason   TEXT,
            size     INTEGER,
            preview  TEXT,          -- first ~400 chars, for the human to judge
            decision TEXT,
            decided  REAL,
            created  REAL
        );
    """)
    db.commit()


# ══════════════════════════════════════════════════════════════════════════════
# 5. WALKING
# ══════════════════════════════════════════════════════════════════════════════

def walk_candidates(media_dir: str):
    """Yield (rel_path, abs_path, Verdict) for every book candidate under
    `media_dir`. Directory context is computed once per directory."""
    for root, dirs, files in os.walk(media_dir):
        base = os.path.basename(root)
        if base.startswith('.'):
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if not d.startswith('.')]

        fileset = set(files)
        ctx = _context_from_listing(fileset, set(dirs))

        # A directory that IS an unpacked book yields one entry (its .opf), not
        # one per chapter — classify() enforces that via the 'part' verdict.
        for f in files:
            if not ext_candidate(f):
                continue
            ap = os.path.join(root, f)
            rp = os.path.relpath(ap, media_dir).replace('\\', '/')
            try:
                v = classify(ap, ctx)
            except Exception as e:
                v = Verdict('triage', None, 'book', f'classify error: {e}')
            yield rp, ap, v


# ══════════════════════════════════════════════════════════════════════════════
# 6. METADATA EXTRACTION
# ══════════════════════════════════════════════════════════════════════════════

_EMPTY_META = {
    'title': '', 'authors': [], 'series': '', 'series_index': None,
    'publisher': '', 'published': '', 'language': '', 'isbn': '',
    'identifiers': {}, 'description': '', 'subjects': [],
    'page_count': None, 'cover_bytes': None, 'source': '',
}


def read_metadata(abs_path: str, fmt: str) -> dict:
    """Normalise metadata from any format into one flat dict. Never raises."""
    meta = dict(_EMPTY_META)
    meta['identifiers'] = {}
    meta['authors'] = []
    meta['subjects'] = []
    try:
        if fmt == 'epub':
            _meta_epub(abs_path, meta)
        elif fmt == 'opf-folder':
            _meta_opf_folder(abs_path, meta)
        elif fmt == 'pdf':
            _meta_pdf(abs_path, meta)
        elif fmt in ('cbz', 'cbr', 'cb7', 'cbt', 'cba'):
            _meta_comic(abs_path, fmt, meta)
        elif fmt == 'fb2':
            _meta_fb2(abs_path, meta)
        elif fmt in ('mobi', 'azw3', 'palmdoc'):
            _meta_mobi(abs_path, meta)
        elif fmt == 'docx':
            _meta_docx(abs_path, meta)
        elif fmt == 'html':
            _meta_html(abs_path, meta)
        elif fmt == 'text':
            _meta_text(abs_path, meta)
    except Exception:
        pass

    if not meta['title']:
        meta['title'] = _title_from_filename(abs_path)
    if not meta['authors']:
        a = _author_from_filename(abs_path)
        if a:
            meta['authors'] = [a]
    if not meta['source']:
        meta['source'] = _guess_source(abs_path, meta)
    return meta


def _title_from_filename(path: str) -> str:
    stem = os.path.splitext(os.path.basename(path))[0]
    # "Author - Title (Series 03)" and "Title - Author" are both everywhere.
    stem = re.sub(r'\s*[\[(]\s*(z-?lib|libgen|epub|retail|v\d+)[^)\]]*[\])]', '',
                  stem, flags=re.I)
    if ' - ' in stem:
        left, right = stem.split(' - ', 1)
        # Heuristic: the side with fewer words and a comma is the author.
        if ',' in left and len(left.split()) <= 4:
            return right.strip()
    return stem.replace('_', ' ').strip()


def _author_from_filename(path: str) -> str:
    stem = os.path.splitext(os.path.basename(path))[0]
    if ' - ' in stem:
        left, right = stem.split(' - ', 1)
        if ',' in left and len(left.split()) <= 4:
            return left.strip()
    return ''


def _guess_source(path: str, meta: dict) -> str:
    p = path.lower()
    ids = meta.get('identifiers') or {}
    if 'ao3' in ids or 'archiveofourown' in p or '/ao3/' in p:
        return 'ao3'
    if 'asin' in ids or 'kindle' in p or 'azw' in p:
        return 'kindle'
    if 'gutenberg' in p or 'pg' in ids:
        return 'gutenberg'
    if 'google' in p and 'play' in p:
        return 'google-play'
    if 'fanfiction' in p or 'ffn' in p:
        return 'ffn'
    return ''


# ── epub ──────────────────────────────────────────────────────────────────────
_DC = '{http://purl.org/dc/elements/1.1/}'
_OPF = '{http://www.idpf.org/2007/opf}'


def _meta_epub(path: str, meta: dict):
    with zipfile.ZipFile(path) as z:
        opf_name = _epub_opf_name(z)
        if not opf_name:
            return
        root = ET.fromstring(z.read(opf_name))
        _parse_opf(root, meta)
        cover = _epub_cover_name(z, root, opf_name)
        if cover:
            try:
                meta['cover_bytes'] = z.read(cover)
            except Exception:
                pass


def _epub_opf_name(z: zipfile.ZipFile) -> str | None:
    try:
        container = ET.fromstring(z.read('META-INF/container.xml'))
        for rf in container.iter():
            if rf.tag.endswith('rootfile') and rf.get('full-path'):
                return rf.get('full-path')
    except Exception:
        pass
    for n in z.namelist():
        if n.lower().endswith('.opf'):
            return n
    return None


def _parse_opf(root: ET.Element, meta: dict):
    for el in root.iter():
        tag = el.tag
        txt = (el.text or '').strip()
        if tag == _DC + 'title' and txt and not meta['title']:
            meta['title'] = txt
        elif tag == _DC + 'creator' and txt:
            role = el.get(_OPF + 'role') or el.get('role') or ''
            if role in ('', 'aut'):
                meta['authors'].append(txt)
        elif tag == _DC + 'publisher' and txt:
            meta['publisher'] = txt
        elif tag == _DC + 'date' and txt and not meta['published']:
            meta['published'] = txt[:10]
        elif tag == _DC + 'language' and txt:
            meta['language'] = txt
        elif tag == _DC + 'description' and txt:
            meta['description'] = _strip_tags(txt)
        elif tag == _DC + 'subject' and txt:
            meta['subjects'].append(txt)
        elif tag == _DC + 'identifier' and txt:
            scheme = (el.get(_OPF + 'scheme') or el.get('scheme') or '').lower()
            if scheme == 'isbn' or txt.lower().startswith('urn:isbn'):
                meta['isbn'] = re.sub(r'[^0-9Xx]', '', txt)[-13:]
            elif scheme:
                meta['identifiers'][scheme] = txt
            else:
                meta['identifiers'].setdefault('uid', txt)
        elif tag == _OPF + 'meta':
            name = (el.get('name') or el.get('property') or '').lower()
            content = el.get('content') or txt
            if not content:
                continue
            if name in ('calibre:series', 'belongs-to-collection'):
                meta['series'] = content
            elif name in ('calibre:series_index', 'group-position'):
                try:
                    meta['series_index'] = float(content)
                except ValueError:
                    pass
    # de-dupe authors, preserve order
    seen, out = set(), []
    for a in meta['authors']:
        if a not in seen:
            seen.add(a)
            out.append(a)
    meta['authors'] = out


def _epub_cover_name(z, opf_root, opf_name) -> str | None:
    base = os.path.dirname(opf_name)
    cover_id = None
    for el in opf_root.iter():
        if el.tag == _OPF + 'meta' and (el.get('name') or '').lower() == 'cover':
            cover_id = el.get('content')
    manifest = {}
    for el in opf_root.iter():
        if el.tag == _OPF + 'item':
            manifest[el.get('id')] = (el.get('href'), el.get('media-type') or '')
    if cover_id and cover_id in manifest:
        href = manifest[cover_id][0]
        if href:
            return _zip_join(base, href, z)
    # properties="cover-image" (epub3)
    for el in opf_root.iter():
        if el.tag == _OPF + 'item' and 'cover-image' in (el.get('properties') or ''):
            return _zip_join(base, el.get('href'), z)
    # last resort: a manifest image whose name says cover
    for _id, (href, mt) in manifest.items():
        if href and mt.startswith('image/') and 'cover' in href.lower():
            return _zip_join(base, href, z)
    return None


def _zip_join(base, href, z) -> str | None:
    if not href:
        return None
    cand = os.path.normpath(os.path.join(base, href)).replace('\\', '/')
    names = set(z.namelist())
    if cand in names:
        return cand
    if href in names:
        return href
    return None


def _meta_opf_folder(opf_path: str, meta: dict):
    try:
        root = ET.parse(opf_path).getroot()
    except Exception:
        return
    _parse_opf(root, meta)
    d = os.path.dirname(opf_path)
    for cand in ('cover.jpg', 'cover.jpeg', 'cover.png'):
        p = os.path.join(d, cand)
        if os.path.exists(p):
            try:
                with open(p, 'rb') as f:
                    meta['cover_bytes'] = f.read()
            except Exception:
                pass
            break


# ── pdf ───────────────────────────────────────────────────────────────────────
def _meta_pdf(path: str, meta: dict):
    doc = _open_pdf(path)
    if doc is None:
        return
    try:
        info = doc.metadata or {}
        meta['title'] = (info.get('title') or '').strip()
        author = (info.get('author') or '').strip()
        if author:
            meta['authors'] = [a.strip() for a in re.split(r'[;&]| and ', author) if a.strip()]
        meta['publisher'] = (info.get('producer') or '').strip()
        meta['description'] = (info.get('subject') or '').strip()
        kw = (info.get('keywords') or '').strip()
        if kw:
            meta['subjects'] = [k.strip() for k in re.split(r'[,;]', kw) if k.strip()]
        meta['page_count'] = doc.page_count
        try:
            pix = doc.load_page(0).get_pixmap(dpi=96)
            meta['cover_bytes'] = pix.tobytes('jpeg') if hasattr(pix, 'tobytes') \
                else pix.getImageData('jpeg')
        except Exception:
            pass
    finally:
        try:
            doc.close()
        except Exception:
            pass


def _open_pdf(path):
    try:
        import fitz            # PyMuPDF
    except Exception:
        return None
    try:
        return fitz.open(path)
    except Exception:
        return None


# ── comic archives ────────────────────────────────────────────────────────────
def comic_page_names(abs_path: str, fmt: str) -> list[str]:
    """Sorted list of page entry names inside a comic archive. Natural sort, so
    page2 < page10 — the single most common complaint about comic readers."""
    names = []
    try:
        if fmt == 'cbz':
            with zipfile.ZipFile(abs_path) as z:
                names = [n for n in z.namelist() if not n.endswith('/')]
        elif fmt == 'cbt':
            with tarfile.open(abs_path) as t:
                names = [m.name for m in t.getmembers() if m.isfile()]
        elif fmt == 'cbr':
            import rarfile
            with rarfile.RarFile(abs_path) as r:
                names = [i.filename for i in r.infolist() if not i.isdir()]
        elif fmt == 'cb7':
            import py7zr
            with py7zr.SevenZipFile(abs_path) as s:
                names = [n for n in s.getnames()]
        elif fmt == 'cba':
            return []          # ACE has no maintained Python reader; needs unace
    except Exception:
        return []
    names = [n for n in names
             if _ext(n) in _PAGE_IMAGE_EXTS and not os.path.basename(n).startswith('.')]
    return sorted(names, key=_natural_key)


def _natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r'(\d+)', s)]


def comic_page_bytes(abs_path: str, fmt: str, name: str) -> bytes | None:
    try:
        if fmt == 'cbz':
            with zipfile.ZipFile(abs_path) as z:
                return z.read(name)
        if fmt == 'cbt':
            with tarfile.open(abs_path) as t:
                f = t.extractfile(name)
                return f.read() if f else None
        if fmt == 'cbr':
            import rarfile
            with rarfile.RarFile(abs_path) as r:
                return r.read(name)
        if fmt == 'cb7':
            import py7zr
            with py7zr.SevenZipFile(abs_path) as s:
                got = s.read([name])
                if got and name in got:
                    return got[name].read()
    except Exception:
        return None
    return None


def _meta_comic(path: str, fmt: str, meta: dict):
    pages = comic_page_names(path, fmt)
    meta['page_count'] = len(pages)
    if pages:
        meta['cover_bytes'] = comic_page_bytes(path, fmt, pages[0])
    # ComicInfo.xml is the de-facto standard sidecar inside cb* archives.
    if fmt == 'cbz':
        try:
            with zipfile.ZipFile(path) as z:
                for n in z.namelist():
                    if os.path.basename(n).lower() == 'comicinfo.xml':
                        _parse_comicinfo(z.read(n), meta)
                        break
        except Exception:
            pass


def _parse_comicinfo(data: bytes, meta: dict):
    try:
        root = ET.fromstring(data)
    except Exception:
        return
    def t(tag):
        el = root.find(tag)
        return (el.text or '').strip() if el is not None and el.text else ''
    if t('Title'):
        meta['title'] = t('Title')
    if t('Series'):
        meta['series'] = t('Series')
        if not meta['title']:
            meta['title'] = t('Series')
    if t('Number'):
        try:
            meta['series_index'] = float(t('Number'))
        except ValueError:
            pass
    for tag in ('Writer', 'Penciller', 'Inker', 'Colorist'):
        v = t(tag)
        if v:
            meta['authors'].extend(a.strip() for a in v.split(',') if a.strip())
    if t('Publisher'):
        meta['publisher'] = t('Publisher')
    if t('Summary'):
        meta['description'] = t('Summary')
    if t('Year'):
        meta['published'] = '-'.join(x for x in (t('Year'), t('Month'), t('Day')) if x)
    if t('Genre'):
        meta['subjects'] = [g.strip() for g in t('Genre').split(',') if g.strip()]
    if t('LanguageISO'):
        meta['language'] = t('LanguageISO')


# ── fb2 ───────────────────────────────────────────────────────────────────────
def _meta_fb2(path: str, meta: dict):
    import base64 as _b64
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return
    ns = {'fb': 'http://www.gribuser.ru/xml/fictionbook/2.0'}
    def find(p):
        el = root.find(p, ns)
        return (el.text or '').strip() if el is not None and el.text else ''
    meta['title'] = find('.//fb:title-info/fb:book-title')
    for a in root.findall('.//fb:title-info/fb:author', ns):
        parts = [(a.find(f'fb:{k}', ns).text or '').strip()
                 for k in ('first-name', 'middle-name', 'last-name')
                 if a.find(f'fb:{k}', ns) is not None]
        name = ' '.join(p for p in parts if p)
        if name:
            meta['authors'].append(name)
    meta['language'] = find('.//fb:title-info/fb:lang')
    ann = root.find('.//fb:title-info/fb:annotation', ns)
    if ann is not None:
        meta['description'] = _strip_tags(ET.tostring(ann, encoding='unicode'))
    for g in root.findall('.//fb:title-info/fb:genre', ns):
        if g.text:
            meta['subjects'].append(g.text.strip())
    seq = root.find('.//fb:title-info/fb:sequence', ns)
    if seq is not None:
        meta['series'] = seq.get('name') or ''
        try:
            meta['series_index'] = float(seq.get('number') or 0) or None
        except ValueError:
            pass
    for b in root.findall('.//fb:binary', ns):
        if (b.get('content-type') or '').startswith('image/') and b.text:
            try:
                meta['cover_bytes'] = _b64.b64decode(b.text)
            except Exception:
                pass
            break


# ── mobi / azw ────────────────────────────────────────────────────────────────
def _meta_mobi(path: str, meta: dict):
    """Parse the MOBI EXTH header directly — no dependency needed for the
    handful of fields that matter, and `mobi`/calibre are only needed for the
    TEXT."""
    try:
        with open(path, 'rb') as f:
            data = f.read(4096)
            # PDB: record0 offset lives at 78
            n_records = struct.unpack('>H', data[76:78])[0]
            rec0_off = struct.unpack('>I', data[78:82])[0]
            f.seek(rec0_off)
            rec0 = f.read(16384)
    except Exception:
        return
    if rec0[16:20] != b'MOBI':
        return
    try:
        header_len = struct.unpack('>I', rec0[20:24])[0]
        exth_flag = struct.unpack('>I', rec0[0x80:0x84])[0]
        # full name
        name_off = struct.unpack('>I', rec0[0x54:0x58])[0]
        name_len = struct.unpack('>I', rec0[0x58:0x5C])[0]
        if name_len and name_off + name_len <= len(rec0):
            meta['title'] = rec0[name_off:name_off + name_len].decode('utf-8', 'replace')
    except Exception:
        return
    if not (exth_flag & 0x40):
        return
    exth_start = 16 + header_len
    if rec0[exth_start:exth_start + 4] != b'EXTH':
        return
    try:
        count = struct.unpack('>I', rec0[exth_start + 8:exth_start + 12])[0]
        pos = exth_start + 12
        EXTH = {100: 'author', 101: 'publisher', 103: 'description',
                104: 'isbn', 105: 'subject', 106: 'published',
                108: 'contributor', 113: 'asin', 503: 'title'}
        for _ in range(count):
            rec_type, rec_len = struct.unpack('>II', rec0[pos:pos + 8])
            payload = rec0[pos + 8:pos + rec_len]
            key = EXTH.get(rec_type)
            if key:
                val = payload.decode('utf-8', 'replace').strip()
                if key == 'author' and val:
                    meta['authors'].append(val)
                elif key == 'subject' and val:
                    meta['subjects'].append(val)
                elif key == 'isbn':
                    meta['isbn'] = re.sub(r'[^0-9Xx]', '', val)
                elif key == 'asin':
                    meta['identifiers']['asin'] = val
                elif key == 'description':
                    meta['description'] = _strip_tags(val)
                elif key == 'published':
                    meta['published'] = val[:10]
                elif key == 'title' and val:
                    meta['title'] = val
                elif key == 'publisher':
                    meta['publisher'] = val
            pos += rec_len
    except Exception:
        pass


# ── docx / html / text ────────────────────────────────────────────────────────
def _meta_docx(path: str, meta: dict):
    try:
        with zipfile.ZipFile(path) as z:
            core = z.read('docProps/core.xml')
    except Exception:
        return
    try:
        root = ET.fromstring(core)
    except Exception:
        return
    CP = '{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}'
    for el in root.iter():
        txt = (el.text or '').strip()
        if not txt:
            continue
        if el.tag == _DC + 'title':
            meta['title'] = txt
        elif el.tag == _DC + 'creator':
            meta['authors'].append(txt)
        elif el.tag == _DC + 'description':
            meta['description'] = txt
        elif el.tag == CP + 'keywords':
            meta['subjects'] = [k.strip() for k in re.split(r'[,;]', txt) if k.strip()]


def _meta_html(path: str, meta: dict):
    head = _read_head(path, 65536).decode('utf-8', 'replace')
    m = re.search(r'<title[^>]*>(.*?)</title>', head, re.I | re.S)
    if m:
        meta['title'] = _html.unescape(_strip_tags(m.group(1))).strip()
    for name, key in (('author', 'author'), ('description', 'description')):
        m = re.search(rf'<meta[^>]+name=["\']{name}["\'][^>]+content=["\'](.*?)["\']',
                      head, re.I | re.S)
        if m:
            v = _html.unescape(m.group(1)).strip()
            if key == 'author':
                meta['authors'].append(v)
            else:
                meta['description'] = v
    if 'archiveofourown.org' in head:
        m = re.search(r'archiveofourown\.org/works/(\d+)', head)
        if m:
            meta['identifiers']['ao3'] = m.group(1)


def _meta_text(path: str, meta: dict):
    head = _read_head(path, 8192).decode('utf-8', 'replace')
    lines = [l.strip() for l in head.splitlines() if l.strip()][:12]
    # Project Gutenberg headers are extremely regular and extremely common.
    for l in lines:
        m = re.match(r'^(?:Title|TITLE):\s*(.+)$', l)
        if m:
            meta['title'] = m.group(1).strip()
        m = re.match(r'^(?:Author|AUTHOR):\s*(.+)$', l)
        if m:
            meta['authors'].append(m.group(1).strip())
        m = re.match(r'^Language:\s*(.+)$', l)
        if m:
            meta['language'] = m.group(1).strip()[:16]
    if not meta['title'] and lines:
        first = lines[0]
        if len(first) < 120:
            meta['title'] = first


# ══════════════════════════════════════════════════════════════════════════════
# 7. TEXT EXTRACTION → SECTIONS
# ══════════════════════════════════════════════════════════════════════════════

class ExtractResult:
    __slots__ = ('status', 'sections', 'error', 'word_count')

    def __init__(self, status, sections=None, error='', word_count=0):
        self.status = status              # ok | needs_backend | failed | unsupported
        self.sections = sections or []    # [{'title':…, 'html':…}]
        self.error = error
        self.word_count = word_count


def extract_sections(abs_path: str, fmt: str) -> ExtractResult:
    """Extract a flow book into sanitized HTML sections.

    Order of attack: a native Python reader if one exists for the format, then
    Calibre. `needs_backend` is a first-class result, not an error — the UI shows
    "install X to read these" rather than a stack trace.
    """
    try:
        if fmt in PAGED_FORMATS:
            return ExtractResult('unsupported', error='paged format — rendered on demand')
        if fmt == 'epub':
            return _extract_epub(abs_path)
        if fmt == 'opf-folder':
            return _extract_opf_folder(abs_path)
        if fmt == 'text':
            return _extract_text(abs_path)
        if fmt == 'html':
            return _extract_html(abs_path)
        if fmt == 'fb2':
            return _extract_fb2(abs_path)
        if fmt == 'docx':
            return _extract_docx(abs_path)
        if fmt == 'rtf':
            return _extract_rtf(abs_path)
    except Exception as e:
        return ExtractResult('failed', error=f'{type(e).__name__}: {e}')

    # Long tail: mobi, azw3, kfx, lit, chm, ceb, lrf, doc, palmdoc…
    return _extract_via_calibre(abs_path, fmt)


def _finish(sections) -> ExtractResult:
    sections = [s for s in sections if s.get('html', '').strip()]
    words = sum(len(re.findall(r"\w+", _strip_tags(s['html']))) for s in sections)
    if not sections:
        return ExtractResult('failed', error='no readable text found')
    return ExtractResult('ok', sections, word_count=words)


def _extract_epub(path: str) -> ExtractResult:
    with zipfile.ZipFile(path) as z:
        opf_name = _epub_opf_name(z)
        order = []
        if opf_name:
            try:
                root = ET.fromstring(z.read(opf_name))
                base = os.path.dirname(opf_name)
                manifest = {}
                for el in root.iter():
                    if el.tag == _OPF + 'item':
                        manifest[el.get('id')] = el.get('href')
                for el in root.iter():
                    if el.tag == _OPF + 'itemref':
                        href = manifest.get(el.get('idref'))
                        j = _zip_join(base, href, z)
                        if j:
                            order.append(j)
            except Exception:
                order = []
        if not order:
            order = [n for n in z.namelist()
                     if _ext(n) in ('.html', '.xhtml', '.htm')]
            order.sort(key=_natural_key)

        sections = []
        for n in order:
            try:
                raw = z.read(n).decode('utf-8', 'replace')
            except Exception:
                continue
            body = _body_of(raw)
            sections.append({'title': _first_heading(body) or os.path.basename(n),
                             'html': sanitize_html(body)})
    return _finish(sections)


def _extract_opf_folder(opf_path: str) -> ExtractResult:
    d = os.path.dirname(opf_path)
    files = []
    try:
        root = ET.parse(opf_path).getroot()
        manifest = {el.get('id'): el.get('href')
                    for el in root.iter() if el.tag == _OPF + 'item'}
        for el in root.iter():
            if el.tag == _OPF + 'itemref':
                href = manifest.get(el.get('idref'))
                if href:
                    files.append(os.path.join(d, href))
    except Exception:
        pass
    if not files:
        files = sorted((os.path.join(d, f) for f in os.listdir(d)
                        if _ext(f) in ('.html', '.xhtml', '.htm')), key=_natural_key)
    sections = []
    for p in files:
        try:
            with open(p, 'r', encoding='utf-8', errors='replace') as f:
                raw = f.read()
        except Exception:
            continue
        body = _body_of(raw)
        sections.append({'title': _first_heading(body) or os.path.basename(p),
                         'html': sanitize_html(body)})
    return _finish(sections)


# Chapter headings in plain-text books. Deliberately conservative: a false split
# just makes an extra section, but a false *merge* on a 900-page book is painful.
_CHAPTER_RE = re.compile(
    r'^\s{0,8}('
    r'chapter\s+(?:\d+|[ivxlcdm]+|one|two|three|four|five|six|seven|eight|nine|ten)'
    r'|part\s+(?:\d+|[ivxlcdm]+)'
    r'|prologue|epilogue|afterword|foreword|introduction'
    r'|\d{1,3}\.'
    r')\b.{0,80}$', re.I)


def _extract_text(path: str) -> ExtractResult:
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        raw = f.read()
    raw = _strip_gutenberg_boilerplate(raw)
    lines = raw.splitlines()

    sections, cur_title, cur = [], '', []
    for line in lines:
        if _CHAPTER_RE.match(line) and len(cur) > 20:
            sections.append({'title': cur_title or 'Start',
                             'html': _paras_to_html(cur)})
            cur_title, cur = line.strip(), []
        else:
            cur.append(line)
    sections.append({'title': cur_title or 'Text', 'html': _paras_to_html(cur)})
    return _finish(sections)


def _strip_gutenberg_boilerplate(text: str) -> str:
    start = re.search(r'\*\*\*\s*START OF (?:THE|THIS) PROJECT GUTENBERG.*?\*\*\*',
                      text, re.I)
    end = re.search(r'\*\*\*\s*END OF (?:THE|THIS) PROJECT GUTENBERG.*?\*\*\*',
                    text, re.I)
    if start:
        text = text[start.end():]
    if end:
        # `end` was found on the original string; re-find on the trimmed one.
        e2 = re.search(r'\*\*\*\s*END OF (?:THE|THIS) PROJECT GUTENBERG.*?\*\*\*',
                       text, re.I)
        if e2:
            text = text[:e2.start()]
    return text


def _paras_to_html(lines: list[str]) -> str:
    out, buf = [], []
    for line in lines:
        if line.strip():
            buf.append(line.strip())
        elif buf:
            out.append('<p>' + _html.escape(' '.join(buf)) + '</p>')
            buf = []
    if buf:
        out.append('<p>' + _html.escape(' '.join(buf)) + '</p>')
    return '\n'.join(out)


def _extract_html(path: str) -> ExtractResult:
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        raw = f.read()
    body = _body_of(raw)
    clean = sanitize_html(body)
    # Split on <h1>/<h2> so a single-file ao3 download with 40 chapters isn't
    # one 900 KB section the reader has to hold in the DOM all at once.
    parts = re.split(r'(?i)(?=<h[12][\s>])', clean)
    sections = []
    for p in parts:
        if not p.strip():
            continue
        sections.append({'title': _first_heading(p) or '', 'html': p})
    if not sections:
        sections = [{'title': '', 'html': clean}]
    return _finish(sections)


def _extract_fb2(path: str) -> ExtractResult:
    try:
        root = ET.parse(path).getroot()
    except Exception as e:
        return ExtractResult('failed', error=str(e))
    ns = {'fb': 'http://www.gribuser.ru/xml/fictionbook/2.0'}
    sections = []
    for body in root.findall('fb:body', ns):
        for sec in body.findall('fb:section', ns):
            title_el = sec.find('fb:title', ns)
            title = _strip_tags(ET.tostring(title_el, encoding='unicode')) \
                if title_el is not None else ''
            paras = []
            for p in sec.iter('{http://www.gribuser.ru/xml/fictionbook/2.0}p'):
                t = ''.join(p.itertext()).strip()
                if t:
                    paras.append('<p>' + _html.escape(t) + '</p>')
            sections.append({'title': title.strip(), 'html': '\n'.join(paras)})
    return _finish(sections)


def _extract_docx(path: str) -> ExtractResult:
    try:
        import docx                      # python-docx
    except Exception:
        return _extract_docx_raw(path)
    try:
        d = docx.Document(path)
    except Exception:
        # python-docx insists on a well-formed OPC package ([Content_Types].xml
        # and friends). Plenty of real-world .docx files — anything produced by
        # a converter, a scraper, or Google Docs export gone wrong — are missing
        # parts python-docx considers mandatory but that still contain perfectly
        # readable text. Fall back to pulling <w:t> runs directly rather than
        # telling the user their manuscript is unreadable.
        return _extract_docx_raw(path)
    sections, cur_title, cur = [], '', []
    for p in d.paragraphs:
        text = p.text.strip()
        style = (p.style.name or '').lower() if p.style else ''
        if style.startswith('heading 1') or style.startswith('heading 2'):
            if cur:
                sections.append({'title': cur_title, 'html': '\n'.join(cur)})
                cur = []
            cur_title = text
            continue
        if text:
            cur.append('<p>' + _html.escape(text) + '</p>')
    sections.append({'title': cur_title, 'html': '\n'.join(cur)})
    return _finish(sections)


def _extract_docx_raw(path: str) -> ExtractResult:
    """python-docx-free fallback: pull <w:t> runs straight out of the XML."""
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read('word/document.xml').decode('utf-8', 'replace')
    except Exception as e:
        return ExtractResult('failed', error=str(e))
    # paragraphs are <w:p>…</w:p>; text is in <w:t>
    paras = []
    for pm in re.finditer(r'<w:p[ >].*?</w:p>', xml, re.S):
        runs = re.findall(r'<w:t[^>]*>(.*?)</w:t>', pm.group(0), re.S)
        text = _html.unescape(''.join(runs)).strip()
        if text:
            paras.append('<p>' + _html.escape(text) + '</p>')
    return _finish([{'title': '', 'html': '\n'.join(paras)}])


def _extract_rtf(path: str) -> ExtractResult:
    try:
        from striprtf.striprtf import rtf_to_text
    except Exception:
        return ExtractResult('needs_backend',
                             error='install `striprtf` to read RTF')
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        text = rtf_to_text(f.read(), errors='ignore')
    return _finish([{'title': '', 'html': _paras_to_html(text.splitlines())}])


# ── Calibre fallback ──────────────────────────────────────────────────────────
_CALIBRE_FORMATS = {'mobi', 'azw3', 'kfx', 'lit', 'chm', 'ceb', 'lrf',
                    'doc', 'palmdoc', 'ereader', 'plucker', 'ztxt', 'opf'}


def have_calibre() -> bool:
    import shutil as _sh
    return _sh.which('ebook-convert') is not None


def _extract_via_calibre(abs_path: str, fmt: str) -> ExtractResult:
    """Universal fallback. `ebook-convert IN OUT.epub` then read the epub.

    This is how the long tail gets handled without this repo growing six more
    fragile format parsers. Calibre is a big install, so it is optional and the
    failure mode is an actionable message.
    """
    if not have_calibre():
        return ExtractResult(
            'needs_backend',
            error=f'{fmt} needs Calibre — install it and make `ebook-convert` '
                  f'available on PATH (apt install calibre)')
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, 'out.epub')
        try:
            p = subprocess.run(['ebook-convert', abs_path, out],
                               capture_output=True, timeout=900)
        except subprocess.TimeoutExpired:
            return ExtractResult('failed', error='ebook-convert timed out (>15 min)')
        if p.returncode != 0 or not os.path.exists(out):
            tail = (p.stderr or b'').decode('utf-8', 'replace')[-400:]
            return ExtractResult('failed', error=f'ebook-convert failed: {tail}')
        return _extract_epub(out)


# ══════════════════════════════════════════════════════════════════════════════
# 8. HTML SANITIZER
# ══════════════════════════════════════════════════════════════════════════════
# Book HTML comes from the internet. It goes straight into our DOM. A tiny
# allowlist sanitizer beats pulling in bleach, and beats trusting epub authors.

_ALLOWED_TAGS = {
    'p', 'br', 'hr', 'div', 'span', 'blockquote', 'pre', 'code',
    'em', 'i', 'strong', 'b', 'u', 's', 'sub', 'sup', 'small', 'mark',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'ul', 'ol', 'li', 'dl', 'dt', 'dd',
    'table', 'thead', 'tbody', 'tr', 'th', 'td', 'caption',
    'a', 'img', 'figure', 'figcaption', 'cite', 'q', 'abbr', 'ruby', 'rt', 'rp',
}
_ALLOWED_ATTRS = {
    'a': {'href', 'title'},
    'img': {'src', 'alt', 'title', 'width', 'height'},
    '*': {'id', 'class', 'lang', 'dir'},
}
_VOID = {'br', 'hr', 'img'}


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'iframe', 'object', 'embed', 'form'):
            self.skip_depth += 1
            return
        if self.skip_depth or tag not in _ALLOWED_TAGS:
            return
        allowed = _ALLOWED_ATTRS.get(tag, set()) | _ALLOWED_ATTRS['*']
        kept = []
        for k, v in attrs:
            k = (k or '').lower()
            if k not in allowed or v is None:
                continue
            if k in ('href', 'src'):
                lv = v.strip().lower()
                # No javascript:, no vbscript:, no data: except images.
                if lv.startswith(('javascript:', 'vbscript:', 'file:')):
                    continue
                if lv.startswith('data:') and not lv.startswith('data:image/'):
                    continue
            kept.append(f'{k}="{_html.escape(v, quote=True)}"')
        self.out.append(f'<{tag}{" " + " ".join(kept) if kept else ""}>')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'iframe', 'object', 'embed', 'form'):
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth or tag not in _ALLOWED_TAGS or tag in _VOID:
            return
        self.out.append(f'</{tag}>')

    def handle_data(self, data):
        if not self.skip_depth:
            self.out.append(_html.escape(data))


def sanitize_html(fragment: str) -> str:
    s = _Sanitizer()
    try:
        s.feed(fragment)
        s.close()
    except Exception:
        return _html.escape(_strip_tags(fragment))
    return ''.join(s.out)


class _Stripper(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.skip = max(0, self.skip - 1)

    def handle_data(self, d):
        if not self.skip:
            self.parts.append(d)


def _strip_tags(s: str) -> str:
    p = _Stripper()
    try:
        p.feed(s or '')
        p.close()
    except Exception:
        return re.sub(r'<[^>]+>', ' ', s or '')
    return re.sub(r'\s+', ' ', ''.join(p.parts)).strip()


def _body_of(raw: str) -> str:
    m = re.search(r'<body[^>]*>(.*)</body>', raw, re.I | re.S)
    return m.group(1) if m else raw


def _first_heading(fragment: str) -> str:
    m = re.search(r'<h[1-6][^>]*>(.*?)</h[1-6]>', fragment or '', re.I | re.S)
    return _strip_tags(m.group(1))[:120] if m else ''


# ══════════════════════════════════════════════════════════════════════════════
# 9. CHUNKING + EMBEDDINGS
# ══════════════════════════════════════════════════════════════════════════════

def chunk_sections(sections: list[dict]) -> list[dict]:
    """Split sections into overlapping ~CHUNK_CHARS passages on sentence
    boundaries. Returns [{'section', 'offset', 'text'}].

    Overlap matters: a passage split mid-scene otherwise buries the very thing
    someone is searching for in the seam between two chunks.
    """
    chunks = []
    for si, sec in enumerate(sections):
        text = _strip_tags(sec.get('html', ''))
        if not text:
            continue
        pos = 0
        n = len(text)
        while pos < n:
            end = min(n, pos + CHUNK_CHARS)
            if end < n:
                # back off to the last sentence end in the final 300 chars
                window = text[max(pos, end - 300):end]
                m = list(re.finditer(r'[.!?]["\')\]]?\s', window))
                if m:
                    end = max(pos, end - 300) + m[-1].end()
            piece = text[pos:end].strip()
            if len(piece) > 80:
                chunks.append({'section': si, 'offset': pos, 'text': piece})
            if end >= n:
                break
            pos = max(pos + 1, end - CHUNK_OVERLAP)
    return chunks


def pack_emb(vec: np.ndarray) -> bytes:
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


def emb_sig(model_tag: str) -> str:
    return f"{EMB_SIG_PREFIX}:{model_tag}"


def rank_by_vector(qv: np.ndarray, rows) -> list[tuple]:
    """Score (rel_path, idx, section, offset, text, emb) rows against a query
    vector. Returns [(score, rel_path, idx, section, offset, text)] descending.
    Vectors from the OAI client are already L2-normalised, so this is a dot."""
    out = []
    q = np.asarray(qv, np.float32).ravel()
    qn = np.linalg.norm(q) or 1.0
    q = q / qn
    for r in rows:
        v = unpack_emb(r['emb'])
        if v is None or v.size != q.size:
            continue
        vn = np.linalg.norm(v) or 1.0
        out.append((float(np.dot(v / vn, q)), r['rel_path'], r['idx'],
                    r['section'], r['offset'], r['text']))
    out.sort(key=lambda t: -t[0])
    return out


# ══════════════════════════════════════════════════════════════════════════════
# 10. MISC
# ══════════════════════════════════════════════════════════════════════════════

def reader_for(fmt: str) -> str:
    return 'paged' if fmt in PAGED_FORMATS else 'flow'


def sort_title(title: str) -> str:
    """'The Hobbit' → 'hobbit, the' so browsing by title isn't 4000 T's."""
    t = (title or '').strip()
    m = re.match(r'^(a|an|the|der|die|das|le|la|les|el|los|las)\s+(.+)$', t, re.I)
    if m:
        return f"{m.group(2)}, {m.group(1)}".lower()
    return t.lower()


def cover_cache_name(rel_path: str) -> str:
    h = hashlib.sha1(rel_path.encode('utf-8')).hexdigest()
    return f"{h}.jpg"


def make_cover_jpeg(data: bytes, max_edge: int = 640) -> bytes | None:
    """Normalise any cover image to a bounded JPEG. cv2 is already a hard dep."""
    if not data:
        return None
    try:
        import cv2
        arr = np.frombuffer(data, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return None
        h, w = img.shape[:2]
        scale = min(1.0, max_edge / max(h, w))
        if scale < 1.0:
            img = cv2.resize(img, (int(w * scale), int(h * scale)),
                             interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return buf.tobytes() if ok else None
    except Exception:
        return None


def render_pdf_page(abs_path: str, page: int, dpi: int = 150) -> bytes | None:
    doc = _open_pdf(abs_path)
    if doc is None:
        return None
    try:
        if page < 0 or page >= doc.page_count:
            return None
        pix = doc.load_page(page).get_pixmap(dpi=dpi)
        return pix.tobytes('jpeg') if hasattr(pix, 'tobytes') else pix.getImageData('jpeg')
    except Exception:
        return None
    finally:
        try:
            doc.close()
        except Exception:
            pass


def page_count_for(abs_path: str, fmt: str) -> int | None:
    if fmt == 'pdf':
        doc = _open_pdf(abs_path)
        if doc is None:
            return None
        try:
            return doc.page_count
        finally:
            try:
                doc.close()
            except Exception:
                pass
    if fmt in COMIC_ARCHIVE_EXTS or fmt in ('cbz', 'cbr', 'cb7', 'cbt', 'cba'):
        return len(comic_page_names(abs_path, fmt))
    return None


def pdf_is_probably_comic(abs_path: str) -> bool:
    """A PDF whose first pages carry almost no extractable text is a scan — i.e.
    a comic/manga rip, not a novel. Decides `kind` so the reader defaults to the
    right chrome (spreads + fit-width vs columns + font size)."""
    doc = _open_pdf(abs_path)
    if doc is None:
        return False
    try:
        n = min(5, doc.page_count)
        if n == 0:
            return False
        total = 0
        for i in range(n):
            try:
                total += len(doc.load_page(i).get_text().strip())
            except Exception:
                pass
        return (total / n) < 120
    except Exception:
        return False
    finally:
        try:
            doc.close()
        except Exception:
            pass