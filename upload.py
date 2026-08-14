"""
Bulk uploader for the AI Media & Asset Manager.

Exit codes:
  0  All files uploaded successfully (or skipped as expected duplicates).
  1  Some files failed after all retries.
  2  All files failed (likely a connectivity or configuration problem).
"""

import os
import sys
import time
import argparse
import getpass
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import requests

# Must match auth.COOKIE_NAME on the server.
COOKIE_NAME = "cim_session"

# Streaming multipart: passing a file handle to requests' files= reads the WHOLE
# file into memory to build the request body, which OOMs on large videos (a 13GB
# camcorder clip is loaded fully, sometimes alongside the server's copy on the
# same box). MultipartEncoder streams the body straight off disk with O(1) peak
# memory. It ships with requests-toolbelt; if that isn't installed we fall back
# to a tiny hand-rolled streaming encoder so large uploads still don't buffer.
try:
    from requests_toolbelt.multipart.encoder import MultipartEncoder  # type: ignore
    _HAVE_TOOLBELT = True
except Exception:                              # pragma: no cover
    MultipartEncoder = None                    # type: ignore
    _HAVE_TOOLBELT = False


class AuthError(Exception):
    """Raised when the uploader cannot obtain or refresh a server session."""


class Session:
    """Holds the uploader's authenticated connection to the server.

    The server (auth.py) uses cookie-based server-side sessions plus a CSRF
    token that every non-GET request must echo in `X-CSRF-Token`. Uploads are
    POSTs, so both are mandatory once auth is enabled.

    Three things this has to get right:

    * ONE session shared by all worker threads. requests.Session is documented
      as not fully thread-safe, but our use is narrow (concurrent POSTs reading
      an already-populated cookie jar), and sharing is what lets every worker
      reuse the connection pool. All *mutation* (login / re-login) is done under
      a lock, so workers never observe a half-updated jar.

    * Re-login on expiry. A big bulk run can outlive the session (default 14
      days is generous, but an admin can revoke, or the server can restart and
      drop its session table). A 401 mid-run must not fail thousands of files,
      so a worker that sees one triggers a single re-login and retries.

    * Auth being OFF must still work. If the server reports auth disabled we
      never send credentials and behave exactly like the old uploader.
    """

    def __init__(self, base_url: str, username: str = "", password: str = "",
                 verify: bool = True):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.http = requests.Session()
        self.http.verify = verify
        self.csrf = ""
        self.auth_enabled = False
        self.user = None
        # Guards login/re-login. Workers hold it only briefly.
        self._lock = threading.Lock()
        # Bumped on every successful login. A worker records the value it used;
        # if a re-login already happened while it was in flight, its 401 is
        # stale and it just retries instead of logging in a second time.
        self._generation = 0

    # -- server capability ---------------------------------------------------
    def probe(self) -> dict:
        """Ask the server whether auth is on. Public endpoint, no session
        needed. A server too old to have /api/auth/config (404) is treated as
        auth-disabled, so this uploader still works against older deployments."""
        url = f"{self.base_url}/api/auth/config"
        try:
            r = self.http.get(url, timeout=30)
        except requests.exceptions.RequestException as e:
            raise AuthError(f"cannot reach server at {self.base_url}: {e}")
        if r.status_code == 404:
            self.auth_enabled = False
            return {"enabled": False, "mode": "none", "legacy": True}
        try:
            cfg = r.json()
        except Exception:
            raise AuthError(
                f"unexpected response from {url} (HTTP {r.status_code}); "
                "is --url pointing at the Media Manager?")
        self.auth_enabled = bool(cfg.get("enabled"))
        return cfg

    # -- login ---------------------------------------------------------------
    def login(self) -> None:
        """Authenticate and store the session cookie + CSRF token."""
        with self._lock:
            self._login_locked()

    def _login_locked(self) -> None:
        if not self.username:
            raise AuthError(
                "server requires authentication but no username was given "
                "(use --username, or set CIM_USERNAME)")
        url = f"{self.base_url}/api/auth/login"
        try:
            r = self.http.post(
                url, json={"username": self.username, "password": self.password},
                timeout=60)
        except requests.exceptions.RequestException as e:
            raise AuthError(f"login request failed: {e}")

        if r.status_code == 401:
            raise AuthError(f"invalid credentials for user {self.username!r}")
        if r.status_code != 200:
            detail = ""
            try:
                detail = r.json().get("error", "")
            except Exception:
                detail = (r.text or "").strip()[:200]
            raise AuthError(f"login failed (HTTP {r.status_code})"
                            + (f": {detail}" if detail else ""))
        try:
            body = r.json()
        except Exception:
            raise AuthError("login succeeded but response was not JSON")

        self.csrf = body.get("csrf", "")
        self.user = body.get("user")
        if not self.csrf:
            raise AuthError("login succeeded but server returned no CSRF token")
        if COOKIE_NAME not in self.http.cookies:
            raise AuthError("login succeeded but no session cookie was set")
        self._generation += 1

    def relogin(self, seen_generation: int) -> bool:
        """Re-authenticate after a 401, unless another thread already did.

        Returns True if a usable session exists afterwards. `seen_generation` is
        the value the caller captured before its request; if the current
        generation has moved past it, someone else already refreshed and the
        caller should simply retry.
        """
        with self._lock:
            if self._generation != seen_generation:
                return True          # already refreshed by another worker
            try:
                self._login_locked()
                return True
            except AuthError as e:
                log_error(f"re-authentication failed: {e}")
                return False

    # -- request helpers -----------------------------------------------------
    @property
    def generation(self) -> int:
        return self._generation

    def headers(self) -> dict:
        """Headers for a state-changing request (adds CSRF when authenticated)."""
        return {"X-CSRF-Token": self.csrf} if self.csrf else {}

    def logout(self) -> None:
        """Best-effort session teardown so we don't leave rows in auth_sessions."""
        if not self.csrf:
            return
        try:
            self.http.post(f"{self.base_url}/api/auth/logout",
                           headers=self.headers(), timeout=15)
        except requests.exceptions.RequestException:
            pass


def log_error(msg: str) -> None:
    print(f"  [!] {msg}", file=sys.stderr)


class _StreamingMultipart:
    """Minimal streaming multipart/form-data body (fallback for when
    requests-toolbelt is absent). Yields the preamble, then the file in fixed
    chunks read lazily from disk, then the epilogue — so the file is never fully
    held in memory. requests accepts any iterable as `data=` and streams it."""

    _CHUNK = 1024 * 1024  # 1 MiB

    def __init__(self, fields: dict, file_field: str, filepath: str, filename: str):
        self.boundary = "----cimuploader" + os.urandom(16).hex()
        self._filepath = filepath
        pre = []
        for name, value in fields.items():
            pre.append(
                f"--{self.boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            )
        pre.append(
            f"--{self.boundary}\r\n"
            f'Content-Disposition: form-data; name="{file_field}"; '
            f'filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        )
        self._preamble = "".join(pre).encode("utf-8")
        self._epilogue = f"\r\n--{self.boundary}--\r\n".encode("utf-8")
        self.content_type = f"multipart/form-data; boundary={self.boundary}"
        self.len = (len(self._preamble)
                    + os.path.getsize(filepath)
                    + len(self._epilogue))

    def __iter__(self):
        yield self._preamble
        with open(self._filepath, "rb") as fh:
            while True:
                chunk = fh.read(self._CHUNK)
                if not chunk:
                    break
                yield chunk
        yield self._epilogue


def _post_streaming(session, endpoint, filepath, fname, form_data, timeout):
    """POST a file as a streamed multipart body without loading it into memory.

    Goes through the Session's requests.Session so the auth cookie rides along,
    and merges in the CSRF header the server demands on non-GET requests.
    """
    http = session.http
    if _HAVE_TOOLBELT:
        fh = open(filepath, "rb")
        try:
            fields = dict(form_data)
            fields["file"] = (fname, fh, "application/octet-stream")
            enc = MultipartEncoder(fields=fields)
            headers = {"Content-Type": enc.content_type}
            headers.update(session.headers())
            return http.post(endpoint, data=enc, headers=headers,
                             timeout=timeout)
        finally:
            fh.close()
    body = _StreamingMultipart(form_data, "file", filepath, fname)
    headers = {"Content-Type": body.content_type,
               "Content-Length": str(body.len)}
    headers.update(session.headers())
    return http.post(endpoint, data=body, headers=headers, timeout=timeout)


# ── Constants ─────────────────────────────────────────────────────────────────

# Media the server will accept: still images + gifs (server converts to jxl,
# animated gifs → animated jxl), video, audio and books (all stored natively).
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.jxl', '.gif', '.apng'}
VIDEO_EXTENSIONS = {'.mp4', '.webm', '.mkv', '.mov', '.avi', '.m4v', '.mpg',
                    '.mpeg', '.wmv', '.flv', '.ts', '.ogv'}
# Audio: stored natively by the server (organised + tagged in place, never
# transcoded). Mirrors server media_types.AUDIO_EXTS — the video-overlapping
# containers (.mp4/.m4a) stay classified as video so a real video is never
# misfiled as a track.
AUDIO_EXTENSIONS = {'.mp3', '.flac', '.aac', '.ogg', '.oga', '.opus',
                    '.wav', '.wma', '.aiff', '.aif'}
# Camera raws: the server develops these into jxl on upload (via rawpy).
RAW_EXTENSIONS = {
    '.dng', '.cr2', '.cr3', '.crw', '.nef', '.nrw', '.arw', '.srf', '.sr2',
    '.raf', '.rw2', '.orf', '.pef', '.ptx', '.raw', '.rwl', '.iiq', '.3fr',
    '.fff', '.mef', '.mos', '.mrw', '.x3f', '.erf', '.kdc', '.dcr',
}
# Books & comics: stored natively by the server, exactly like video and audio.
#
# ONLY the unambiguous extensions are here, mirroring
# media_types.UNAMBIGUOUS_BOOK_EXTS. The server accepts nothing else, and the
# reason is worth stating because it looks like an omission:
#
#   .txt  is also this app's tag-sidecar format
#   .htm(l) is also a saved webpage, or one chapter of an unpacked epub
#   .pdb  is also a generic Palm database
#   .opf  is a manifest whose *folder* is the book
#   .doc  is any OLE2 compound document
#
# Deciding those needs the file's bytes AND its neighbours on disk, which is a
# judgement only the server can make (book_index.classify). Uploading them
# blindly would turn every tag sidecar in a source tree into a "book". Put text
# books directly in the media folder and let the server's indexer classify them
# in context; that path has the triage queue for the genuinely ambiguous ones.
BOOK_EXTENSIONS = {
    '.epub', '.mobi', '.azw', '.azw3', '.kf8', '.kfx', '.lit', '.fb2',
    '.lrf', '.lrx', '.chm', '.ceb', '.docx', '.rtf', '.pdf',
    '.cbz', '.cbr', '.cb7', '.cbt', '.cba',
}
MEDIA_EXTENSIONS = (IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | RAW_EXTENSIONS
                    | AUDIO_EXTENSIONS | BOOK_EXTENSIONS)

# Files we never want to walk even in --aggressive mode: sidecars and the
# uploader's own bookkeeping. Everything else is fair game when aggressive.
NON_MEDIA_EXTENSIONS = {'.txt', '.xmp', '.json', '.md', '.ini', '.log', '.db'}

# Error codes the server sends — determines retry behaviour.
# Permanent: don't retry; the file will never succeed as-is.
# Temporary: transient failure; worth retrying with backoff.
PERMANENT_ERROR_CODES = {
    "exact_duplicate",    # SHA-256 match — file already exists
    "filename_exists",    # same name in same folder (not a content check)
    "bad_folder",         # folder path rejected by server
    "no_file",            # shouldn't happen, but treat as permanent
    "conversion_failed",  # cjxl rejected the file — bad image data
}
TEMPORARY_ERROR_CODES = {
    "server_error",       # unhandled exception on server
}
# Auth failures are their own class: not permanent (a fresh login usually fixes
# them) but not blind-retryable either — retrying without re-authenticating just
# burns attempts. These are handled by re-login + retry inside upload_file.
AUTH_STATUS_CODES = {401, 403}
# Any HTTP 5xx or network error is also treated as temporary.


# ── Result types ──────────────────────────────────────────────────────────────

class Outcome(Enum):
    SUCCESS   = "success"
    DUPLICATE = "duplicate"    # exact_duplicate or filename_exists
    SKIPPED   = "skipped"      # other permanent rejection
    FAILED    = "failed"       # gave up after retries


@dataclass
class UploadResult:
    filepath:      str
    outcome:       Outcome
    message:       str
    error_code:    Optional[str] = None
    existing_file: Optional[str] = None
    attempts:      int = 1


# ── Sidecar parsing ───────────────────────────────────────────────────────────

def load_classes(source_dir: str) -> list[str]:
    p = os.path.join(source_dir, "classes.txt")
    if os.path.exists(p):
        with open(p, encoding='utf-8') as f:
            return [l.strip() for l in f if l.strip()]
    return []


def parse_sidecar(filepath: str, classes_map: list[str]) -> tuple:
    """
    Returns (regions, description, tags).
    Supports three sidecar formats:
      1. Pipe-separated tags:  tag1|tag2|description: some text
      2. YOLO label format:    <class_id> <cx> <cy> <w> <h>
      3. Fallback:             entire file content as description
    """
    sidecar = os.path.splitext(filepath)[0] + ".txt"
    # A .txt book is its own "sidecar" by this naming rule — `moby.txt` would
    # have the entire novel read in as its description, and a YOLO-format parse
    # attempt on 200 KB of prose on top. Never let a file be its own sidecar.
    if os.path.abspath(sidecar) == os.path.abspath(filepath):
        return [], "", []
    # Books carry their own metadata (epub OPF, ComicInfo.xml, MOBI EXTH, PDF
    # info dict), which the server reads on ingest. A .txt beside a .epub is far
    # more likely to be a stray note than tags for that book, and letting it
    # through would overwrite real publisher metadata with a filename dump.
    if os.path.splitext(filepath)[1].lower() in BOOK_EXTENSIONS:
        return [], "", []
    if not os.path.exists(sidecar):
        return [], "", []
    try:
        content = open(sidecar, encoding='utf-8').read().strip()
    except Exception as e:
        print(f"  [!] Could not read sidecar {sidecar}: {e}")
        return [], "", []
    if not content:
        return [], "", []

    # Format 1: pipe-separated
    if content.count('|') > 1:
        tags, desc_parts = [], []
        for t in [x.strip() for x in content.split('|') if x.strip()]:
            tl = t.lower()
            if tl.startswith('description:'):
                clean = t[12:].strip()
                if clean: desc_parts.append(clean)
            elif len(t) > 20:
                desc_parts.append(t)
            else:
                tags.append(t)
        return [], "; ".join(desc_parts), tags

    # Format 2: YOLO
    lines = [l.strip() for l in content.split('\n') if l.strip()]
    regions = []
    is_yolo = True
    for line in lines:
        parts = line.split()
        if len(parts) != 5:
            is_yolo = False; break
        try:
            cid = int(parts[0])
            cx, cy, w, h = map(float, parts[1:])
            if not all(0.0 <= v <= 1.0 for v in (cx, cy, w, h)):
                is_yolo = False; break
            name = classes_map[cid] if cid < len(classes_map) else f"class_{cid}"
            regions.append({"class_name": name, "cx": cx, "cy": cy, "w": w, "h": h})
        except ValueError:
            is_yolo = False; break
    if is_yolo and regions:
        return regions, "", []

    # Format 3: description fallback
    return [], content, []


# ── Upload logic ──────────────────────────────────────────────────────────────

def upload_file(
    filepath:        str,
    source_dir:      str,
    classes_map:     list[str],
    endpoint:        str,
    max_attempts:    int,
    initial_backoff: float,
    session:         "Session",
    dest:            str = "",
) -> UploadResult:
    rel_dir = os.path.relpath(os.path.dirname(filepath), source_dir)
    parts   = [p for p in (dest, rel_dir if rel_dir != "." else "") if p]
    folder  = "/".join(parts).replace('\\', '/')
    fname   = os.path.basename(filepath)

    regions, description, tags = parse_sidecar(filepath, classes_map)
    metadata = {}
    if regions or description or tags:
        metadata = {"tags": tags, "description": description, "regions": regions}

    last_error  = ""
    last_code   = ""
    last_detail = ""

    for attempt in range(1, max_attempts + 1):
        try:
            form_data = {'folder': folder}
            if metadata:
                form_data['metadata'] = json.dumps(metadata)
            # Stream the file off disk instead of buffering it — a 13GB video
            # uploads with ~1 MiB peak memory instead of loading fully (twice,
            # counting the server's copy on a shared box).
            # Capture the session generation BEFORE sending, so a 401 can be
            # told apart from "another thread already re-logged in".
            gen = session.generation
            resp = _post_streaming(session, endpoint, filepath, fname,
                                   form_data, timeout=180)

            # Parse response
            try:
                body = resp.json()
            except Exception:
                body = {}

            if resp.status_code == 200 and body.get('success'):
                # The server may have corrected a mislabeled extension; surface
                # that rather than silently reporting a plain success.
                corrected = body.get('corrected_extension') or {}
                note = ""
                if corrected:
                    note = (f"  [type corrected {corrected.get('from') or '(none)'}"
                            f" → {corrected.get('to')}]")
                return UploadResult(
                    filepath=filepath,
                    outcome=Outcome.SUCCESS,
                    message=f"→ {body.get('filename', fname)}{note}",
                    attempts=attempt,
                )

            error_code = body.get('error_code', '')
            error_msg  = body.get('error', f"HTTP {resp.status_code}")
            detail     = body.get('detail', '')
            existing   = body.get('existing_file')

            # Session expired / revoked / CSRF rejected. Re-authenticate once
            # and retry — otherwise a long run that outlives its session would
            # fail every remaining file. This MUST be checked before the generic
            # 4xx branch below, which would otherwise mark it permanently
            # skipped and silently drop the file.
            if resp.status_code in AUTH_STATUS_CODES and session.auth_enabled:
                if session.relogin(gen):
                    last_error = f"session expired; re-authenticated ({error_msg})"
                    last_code  = "auth_retry"
                    # Retry immediately: this isn't server load, it's a
                    # credential refresh, so backoff would just waste time.
                    continue
                return UploadResult(
                    filepath=filepath,
                    outcome=Outcome.FAILED,
                    message="authentication failed and could not be renewed",
                    error_code="auth_failed",
                    attempts=attempt,
                )

            # Duplicate — permanent, specific outcome
            if error_code in ('exact_duplicate', 'filename_exists'):
                msg = f"duplicate of {existing}" if existing else error_msg
                return UploadResult(
                    filepath=filepath,
                    outcome=Outcome.DUPLICATE,
                    message=msg,
                    error_code=error_code,
                    existing_file=existing,
                    attempts=attempt,
                )

            # Other permanent errors — no retry
            if error_code in PERMANENT_ERROR_CODES or (
                400 <= resp.status_code < 500 and resp.status_code != 408
            ):
                msg = error_msg
                if detail:
                    msg += f" ({detail})"
                return UploadResult(
                    filepath=filepath,
                    outcome=Outcome.SKIPPED,
                    message=msg,
                    error_code=error_code,
                    attempts=attempt,
                )

            # Temporary — will retry
            last_error  = error_msg
            last_code   = error_code
            last_detail = detail

        except requests.exceptions.Timeout:
            last_error = "request timed out"
            last_code  = "timeout"
        except requests.exceptions.ConnectionError as e:
            last_error = f"connection error: {e}"
            last_code  = "connection_error"
        except Exception as e:
            last_error = str(e)
            last_code  = "client_error"

        # Backoff before retry (not after last attempt)
        if attempt < max_attempts:
            backoff = initial_backoff * (2 ** (attempt - 1))
            time.sleep(backoff)

    # Exhausted retries
    msg = f"gave up after {max_attempts} attempt(s): {last_error}"
    if last_detail:
        msg += f" ({last_detail})"
    return UploadResult(
        filepath=filepath,
        outcome=Outcome.FAILED,
        message=msg,
        error_code=last_code,
        attempts=max_attempts,
    )


# ── Summary printing ──────────────────────────────────────────────────────────

def print_summary(results: list[UploadResult], verbose_duplicates: bool) -> None:
    by_outcome: dict[Outcome, list[UploadResult]] = {o: [] for o in Outcome}
    for r in results:
        by_outcome[r.outcome].append(r)

    total     = len(results)
    succeeded = len(by_outcome[Outcome.SUCCESS])
    dupes     = len(by_outcome[Outcome.DUPLICATE])
    skipped   = len(by_outcome[Outcome.SKIPPED])
    failed    = len(by_outcome[Outcome.FAILED])

    print("\n" + "─" * 60)
    print(f"  Total:      {total}")
    print(f"  Uploaded:   {succeeded}")
    print(f"  Duplicates: {dupes}  (skipped — already on server)")
    print(f"  Skipped:    {skipped}  (permanent rejection)")
    print(f"  Failed:     {failed}  (gave up after retries)")
    print("─" * 60)

    if verbose_duplicates and by_outcome[Outcome.DUPLICATE]:
        print("\nDuplicate files:")
        for r in by_outcome[Outcome.DUPLICATE]:
            fname = os.path.basename(r.filepath)
            if r.existing_file:
                print(f"  {fname}  →  exists as  {r.existing_file}")
            else:
                print(f"  {fname}  (filename conflict)")

    if by_outcome[Outcome.SKIPPED]:
        print("\nPermanently rejected files:")
        for r in by_outcome[Outcome.SKIPPED]:
            print(f"  {os.path.basename(r.filepath)}: [{r.error_code}] {r.message}")

    if by_outcome[Outcome.FAILED]:
        print("\nFiles that failed after all retries:")
        for r in by_outcome[Outcome.FAILED]:
            print(f"  {os.path.basename(r.filepath)}: {r.message}")


# ── Entry point ───────────────────────────────────────────────────────────────

def _should_upload(fname: str, aggressive: bool) -> bool:
    ext = os.path.splitext(fname)[1].lower()
    if aggressive:
        # Upload anything that isn't obviously a sidecar / bookkeeping file, even
        # if it has no extension or a wrong one — the server will try to convert
        # it and reject cleanly (conversion_failed) if it truly can't.
        if fname == "classes.txt":
            return False
        return ext not in NON_MEDIA_EXTENSIONS
    return ext in MEDIA_EXTENSIONS


def bulk_upload(
    source_dir:      str,
    server_url:      str,
    workers:         int,
    max_attempts:    int,
    initial_backoff: float,
    verbose_dupes:   bool,
    aggressive:      bool = False,
    username:        str = "",
    password:        str = "",
    verify_tls:      bool = True,
    dest:            str = "",
) -> int:
    source_dir = os.path.abspath(source_dir)
    if not os.path.isdir(source_dir):
        print(f"Error: '{source_dir}' is not a directory.")
        return 2

    dest = "/".join(
        s for s in dest.replace('\\', '/').split('/')
        if s and s not in ('.', '..')
    )

    session = Session(server_url, username, password, verify=verify_tls)
    try:
        cfg = session.probe()
    except AuthError as e:
        print(f"Error: {e}")
        return 2

    if session.auth_enabled:
        if cfg.get("needs_bootstrap"):
            print("[*] Server has no users yet — this login will create the "
                  "initial admin account.")
        if not session.username:
            print("Error: server requires authentication. Pass --username "
                  "(and --password, or set CIM_PASSWORD, or be prompted).")
            return 2
        try:
            session.login()
        except AuthError as e:
            print(f"Error: {e}")
            return 2
        who = (session.user or {}).get("username", session.username)
        admin = " (admin)" if (session.user or {}).get("is_admin") else ""
        print(f"[*] Authenticated as {who}{admin} (mode: {cfg.get('mode','?')}).")
    elif username:
        # Credentials supplied but the server doesn't want them. Say so rather
        # than silently ignoring the flag — it usually means --url is wrong.
        print("[*] Server has authentication disabled; ignoring --username.")

    classes_map = load_classes(source_dir)
    files = [
        os.path.join(root, f)
        for root, _, filenames in os.walk(source_dir)
        for f in filenames
        if _should_upload(f, aggressive)
    ]
    if not files:
        print("No media files found.")
        return 0

    if aggressive:
        print("[*] Aggressive mode: uploading all non-sidecar files, including "
              "misnamed / extension-less ones (server will attempt conversion).")

    endpoint = f"{server_url.rstrip('/')}/api/upload"
    total    = len(files)
    print(f"[*] Found {total} file(s).  Server: {endpoint}")
    if max_attempts > 1:
        print(f"[*] Retries: up to {max_attempts} attempts, "
              f"{initial_backoff}s initial backoff (exponential).\n")
    else:
        print()

    results: list[UploadResult] = []
    completed = 0

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(
                upload_file, fp, source_dir, classes_map,
                endpoint, max_attempts, initial_backoff, session, dest
            ): fp
            for fp in files
        }
        for future in as_completed(futures):
            completed += 1
            r = future.result()
            results.append(r)

            # Per-file status line
            icon = {"success":"✓","duplicate":"=","skipped":"!","failed":"✗"}[r.outcome.value]
            fname = os.path.basename(r.filepath)
            atts  = f" (attempt {r.attempts})" if r.attempts > 1 else ""
            print(f"  [{completed:>{len(str(total))}}/{total}] {icon} {fname}{atts}  {r.message}")

    # Release the server-side session instead of leaving it to expire.
    session.logout()

    print_summary(results, verbose_dupes)

    failed_count = sum(1 for r in results if r.outcome == Outcome.FAILED)
    if failed_count == total:
        return 2
    if failed_count > 0:
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bulk-upload images to the AI Media & Asset Manager.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("source_dir",
        help="Local folder to upload (recursively).")
    parser.add_argument("--url", default="http://localhost:8000",
        help="Base URL of the Media Manager server.")
    parser.add_argument("--dest", default="",
        help="Destination folder on the server to nest uploads under. The "
             "source's own subfolders are preserved beneath it, e.g. "
             "--dest myphotos uploads photos/cat.jpg to myphotos/photos/cat.jpg.")
    parser.add_argument("--workers", type=int, default=8,
        help="Number of concurrent uploads.")
    parser.add_argument("--retries", type=int, default=3,
        help="Max attempts per file for transient errors (1 = no retry).")
    parser.add_argument("--backoff", type=float, default=2.0,
        help="Initial retry backoff in seconds (doubles each attempt).")
    parser.add_argument("--verbose-duplicates", action="store_true",
        help="List every duplicate with its existing server path in the summary.")
    parser.add_argument("--aggressive", action="store_true",
        help="Upload every file (except sidecars), including ones with a wrong "
             "or missing extension, and let the server try to convert them. "
             "Useful for recovering misnamed media.")

    auth_group = parser.add_argument_group("authentication")
    auth_group.add_argument("--username", default=os.environ.get("CIM_USERNAME", ""),
        help="Username for servers with authentication enabled. "
             "Defaults to $CIM_USERNAME.")
    auth_group.add_argument("--password", default=None,
        help="Password. Prefer $CIM_PASSWORD or the interactive prompt — a "
             "password passed here is visible in ps output and shell history.")
    auth_group.add_argument("--password-file", default=None,
        help="Read the password from this file (first line). Safer than "
             "--password for scripts and cron jobs.")
    auth_group.add_argument("--no-verify-tls", action="store_true",
        help="Skip TLS certificate verification (self-signed https servers).")
    args = parser.parse_args()

    # Password resolution, most to least secure:
    #   --password-file  ->  $CIM_PASSWORD  ->  --password  ->  interactive
    # Only prompt when we have a username, a tty, and nothing else supplied;
    # otherwise a cron job would hang forever waiting on stdin.
    password = ""
    if args.password_file:
        try:
            with open(args.password_file, "r", encoding="utf-8") as fh:
                password = fh.readline().strip()
        except OSError as e:
            print(f"Error: cannot read --password-file: {e}")
            sys.exit(2)
    elif os.environ.get("CIM_PASSWORD"):
        password = os.environ["CIM_PASSWORD"]
    elif args.password is not None:
        password = args.password
    elif args.username and sys.stdin.isatty():
        try:
            password = getpass.getpass(f"Password for {args.username}: ")
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(2)

    if args.no_verify_tls:
        # Silence the per-request InsecureRequestWarning spam; the user opted in.
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass

    sys.exit(bulk_upload(
        source_dir      = args.source_dir,
        server_url      = args.url,
        workers         = args.workers,
        max_attempts    = args.retries,
        initial_backoff = args.backoff,
        verbose_dupes   = args.verbose_duplicates,
        aggressive      = args.aggressive,
        username        = args.username,
        password        = password,
        verify_tls      = not args.no_verify_tls,
        dest            = args.dest,
    ))


if __name__ == "__main__":
    main()