"""! @file
@brief Command-line bulk uploader (standard library only, Python 3.6+).

Runs on an old machine with nothing but Python: urllib.request, a small
streaming multipart encoder and, for big files, the server's chunked upload
sessions (/api/upload/session) with resume after a dropped connection.

Exit codes: 0 all uploaded (or expected duplicates), 1 some failed after
retries, 2 all failed (connection or configuration).
"""

import argparse
import getpass
import hashlib
import http.client
import http.cookiejar
import json
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import Enum

# same as auth.COOKIE_NAME on the server
COOKIE_NAME = "cim_session"
## @brief Files above this go through chunked sessions when the server offers them.
CHUNKED_OVER = 8 * 1024 * 1024
## @brief Consecutive failed chunk sends before a chunked upload gives up (the
# file-level retry then resumes the same session).
CHUNK_RETRIES = 5


class AuthError(Exception):
    """! @brief The uploader could not get or refresh a server session."""


class TransportError(Exception):
    """! @brief The request never got an HTTP answer (connection refused / reset, timeout)."""


def log_error(msg):
    """! @brief Print a warning line to stderr."""
    print("  [!] {}".format(msg), file=sys.stderr)


def _json(raw):
    """! @brief Decode a JSON body, {} when it is not JSON."""
    try:
        out = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        return out if isinstance(out, dict) else {}
    except Exception:
        return {}


class Session(object):
    """! @brief The uploader's authenticated connection: session cookie plus the CSRF
    token every POST / PUT must echo.

    One opener (with a cookie jar) is shared by all workers; login and re-login
    happen under a lock. A 401 mid-run triggers one re-login and a retry. With
    auth off on the server, no credentials are sent.
    """

    def __init__(self, base_url, username="", password="", verify=True, opener=None):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.jar = http.cookiejar.CookieJar()
        handlers = [urllib.request.HTTPCookieProcessor(self.jar)]
        if not verify:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        self.opener = opener or urllib.request.build_opener(*handlers)
        self.csrf = ""
        self.auth_enabled = False
        self.user = None
        self.config = {}
        self._lock = threading.Lock()
        # Bumped per login, so a 401 from a request sent before someone else's
        # re-login just retries.
        self._generation = 0

    def raw(self, method, path, body=None, headers=None, timeout=60):
        """! @brief One HTTP request, no auth handling.
        @param body  bytes, a file-like object with read() (Content-Length in
                     headers), or None.
        @return (status, response bytes).
        @throws TransportError when no HTTP answer came back.
        """
        req = urllib.request.Request(self.base_url + path, data=body,
                                     headers=dict(headers or {}), method=method)
        try:
            resp = self.opener.open(req, timeout=timeout)
            try:
                return resp.getcode(), resp.read()
            finally:
                resp.close()
        except urllib.error.HTTPError as e:
            try:
                return e.code, e.read()
            finally:
                e.close()
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            raise TransportError(str(getattr(e, "reason", None) or e))

    def request(self, method, path, body=None, headers=None, timeout=60):
        """! @brief A request with the CSRF header, re-logging in once on a 401 / 403.
        @param body  bytes, or a zero-argument callable returning a fresh body (a
                     stream can only be sent once, a retry needs a new one).
        @return (status, decoded JSON dict).
        """
        for attempt in (1, 2):
            gen = self._generation
            h = dict(headers or {})
            if method not in ("GET", "HEAD"):
                h.update(self.headers())
            data = body() if callable(body) else body
            status, raw = self.raw(method, path, data, h, timeout)
            if status in AUTH_STATUS_CODES and self.auth_enabled and attempt == 1:
                if self.relogin(gen):
                    continue
            return status, _json(raw)
        return status, _json(raw)

    __call__ = request

    def probe(self):
        """! @brief Ask whether the server has auth on (an old server without the endpoint counts as off)."""
        try:
            status, raw = self.raw("GET", "/api/auth/config", timeout=30)
        except TransportError as e:
            raise AuthError("cannot reach server at {}: {}".format(self.base_url, e))
        if status == 404:
            self.auth_enabled = False
            return {"enabled": False, "mode": "none", "legacy": True}
        cfg = _json(raw)
        if not cfg:
            raise AuthError(
                "unexpected response from {}/api/auth/config (HTTP {}); "
                "is --url pointing at the Media Manager?".format(self.base_url, status))
        self.auth_enabled = bool(cfg.get("enabled"))
        return cfg

    def server_config(self):
        """! @brief The server's upload offer ({} from an old server without sessions)."""
        try:
            status, body = self.request("GET", "/api/upload/config", timeout=30)
        except TransportError:
            return {}
        self.config = body if status == 200 and body.get("success") else {}
        return self.config

    def login(self):
        """! @brief Log in; store the session cookie and CSRF token."""
        with self._lock:
            self._login_locked()

    def _login_locked(self):
        if not self.username:
            raise AuthError(
                "server requires authentication but no username was given "
                "(use --username, or set CIM_USERNAME)")
        payload = json.dumps({"username": self.username,
                              "password": self.password}).encode("utf-8")
        try:
            status, raw = self.raw("POST", "/api/auth/login", payload,
                                   {"Content-Type": "application/json"}, timeout=60)
        except TransportError as e:
            raise AuthError("login request failed: {}".format(e))
        body = _json(raw)
        if status == 401:
            raise AuthError("invalid credentials for user {!r}".format(self.username))
        if status != 200:
            detail = body.get("error", "") or raw.decode("utf-8", "replace").strip()[:200]
            raise AuthError("login failed (HTTP {})".format(status)
                            + (": {}".format(detail) if detail else ""))
        if not body:
            raise AuthError("login succeeded but response was not JSON")
        self.csrf = body.get("csrf", "")
        self.user = body.get("user")
        if not self.csrf:
            raise AuthError("login succeeded but server returned no CSRF token")
        if not any(c.name == COOKIE_NAME for c in self.jar):
            raise AuthError("login succeeded but no session cookie was set")
        self._generation += 1

    def relogin(self, seen_generation):
        """! @brief Log in again after a 401, unless another worker already did.
        @param seen_generation  the generation the caller's request used.
        @return True when a usable session exists.
        """
        with self._lock:
            if self._generation != seen_generation:
                return True
            try:
                self._login_locked()
                return True
            except AuthError as e:
                log_error("re-authentication failed: {}".format(e))
                return False

    @property
    def generation(self):
        return self._generation

    def headers(self):
        """! @brief Headers for a POST / PUT (CSRF when signed in)."""
        return {"X-CSRF-Token": self.csrf} if self.csrf else {}

    def logout(self):
        """! @brief End the server-side session (best effort)."""
        if not self.csrf:
            return
        try:
            self.raw("POST", "/api/auth/logout", b"", self.headers(), timeout=15)
        except TransportError:
            pass


def _quote_param(value):
    """! @brief A multipart header parameter value, quoted (quotes and newlines escaped)."""
    return str(value).replace("\\", "\\\\").replace('"', "%22").replace("\r", "%0D").replace("\n", "%0A")


class StreamingMultipart(object):
    """! @brief A multipart/form-data body streamed from disk: preamble, the file in
    1 MiB pieces, epilogue. Iterable, and file-like (read(n)) for http.client.
    """

    _CHUNK = 1024 * 1024

    def __init__(self, fields, file_field, filepath, filename, boundary=None):
        self.boundary = boundary or "----cimuploader" + os.urandom(16).hex()
        self._filepath = filepath
        pre = []
        for name, value in fields.items():
            pre.append(
                "--{b}\r\nContent-Disposition: form-data; name=\"{n}\"\r\n\r\n{v}\r\n".format(
                    b=self.boundary, n=_quote_param(name), v=value))
        pre.append(
            "--{b}\r\nContent-Disposition: form-data; name=\"{n}\"; filename=\"{f}\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n".format(
                b=self.boundary, n=_quote_param(file_field), f=_quote_param(filename)))
        self._preamble = "".join(pre).encode("utf-8")
        self._epilogue = "\r\n--{}--\r\n".format(self.boundary).encode("utf-8")
        self.content_type = "multipart/form-data; boundary={}".format(self.boundary)
        self.len = len(self._preamble) + os.path.getsize(filepath) + len(self._epilogue)
        self._stage = 0  # read(): 0 preamble, 1 file, 2 epilogue, 3 done
        self._buf = b""
        self._fh = None

    def __iter__(self):
        yield self._preamble
        with open(self._filepath, "rb") as fh:
            while True:
                chunk = fh.read(self._CHUNK)
                if not chunk:
                    break
                yield chunk
        yield self._epilogue

    def read(self, n=-1):
        """! @brief File-like read for http.client's streaming send."""
        if n is None or n < 0:
            n = self.len
        out = []
        want = n
        while want > 0 and self._stage < 3:
            if not self._buf:
                if self._stage == 0:
                    self._buf, self._stage = self._preamble, 1
                    self._fh = open(self._filepath, "rb")
                elif self._stage == 1:
                    self._buf = self._fh.read(self._CHUNK)
                    if not self._buf:
                        self._fh.close()
                        self._buf, self._stage = self._epilogue, 2
                else:
                    self._stage = 3
                    break
            piece, self._buf = self._buf[:want], self._buf[want:]
            out.append(piece)
            want -= len(piece)
        return b"".join(out)

    def headers(self):
        """! @brief Content-Type and Content-Length for this body."""
        return {"Content-Type": self.content_type, "Content-Length": str(self.len)}


def file_sha256(path):
    """! @brief The hex sha256 of a file, read in 1 MiB pieces."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            piece = fh.read(1024 * 1024)
            if not piece:
                break
            h.update(piece)
    return h.hexdigest()


def chunked_upload(http, filepath, fname, folder, metadata, mode, validate=False,
                   backoff=1.0, resume=None, progress=None):
    """! @brief Send one file through an upload session, resuming after drops.
    @param http      callable(method, path, body=, headers=, timeout=) -> (status, dict);
                     a Session, or a fake in tests.
    @param resume    dict filepath -> session id, so a later attempt in this run
                     continues the same session.
    @param progress  fn(bytes_sent) after each stored chunk.
    @return (status, body) of the final answer (the receipt, a duplicate, an error),
            or None when the server has no sessions (fall back to /api/upload).
    @throws TransportError after CHUNK_RETRIES consecutive failed sends.
    """
    resume = resume if resume is not None else {}
    size = os.path.getsize(filepath)
    sid, offset, chunk = resume.get(filepath), 0, 0
    if sid:
        status, body = http("GET", "/api/upload/session/" + sid, timeout=30)
        if status == 200 and body.get("success"):
            offset, chunk = int(body.get("received") or 0), int(body.get("chunk_size") or 0)
        else:
            sid = None
    if not sid:
        req = {"filename": fname, "size": size, "folder": folder, "mode": mode}
        if metadata:
            req["metadata"] = metadata
        if validate:
            req["sha256"] = file_sha256(filepath)
        status, body = http("POST", "/api/upload/session",
                            body=json.dumps(req).encode("utf-8"),
                            headers={"Content-Type": "application/json"}, timeout=60)
        if status in (404, 405):
            return None
        if status >= 400 or not body.get("success") or not body.get("id"):
            return status, body  # a refusal, or a duplicate answered up front
        sid, offset = body["id"], int(body.get("received") or 0)
        chunk = int(body.get("chunk_size") or 0)
        resume[filepath] = sid
    chunk = chunk if chunk > 0 else CHUNKED_OVER
    failures = 0
    with open(filepath, "rb") as fh:
        while offset < size:
            fh.seek(offset)
            data = fh.read(chunk)
            try:
                status, body = http("PUT", "/api/upload/session/{}?offset={}".format(sid, offset),
                                    body=data, headers={"Content-Type": "application/octet-stream",
                                                        "Content-Length": str(len(data))},
                                    timeout=300)
            except TransportError:
                failures += 1
                if failures >= CHUNK_RETRIES:
                    raise
                time.sleep(backoff * failures)
                # ask the server how much arrived, then go on from there
                try:
                    status, body = http("GET", "/api/upload/session/" + sid, timeout=30)
                except TransportError:
                    continue
                if status != 200:
                    resume.pop(filepath, None)
                    return status, body
                offset = int(body.get("received") or 0)
                continue
            if status == 409 and "received" in body:
                offset = int(body["received"])  # the server's count wins
                continue
            if status >= 400:
                if status == 404:
                    resume.pop(filepath, None)  # expired: the next attempt starts over
                return status, body
            failures = 0
            offset = int(body.get("received", offset + len(data)))
            if progress:
                progress(offset)
    status, body = http("POST", "/api/upload/session/{}/complete".format(sid),
                        body=json.dumps({"mode": mode}).encode("utf-8"),
                        headers={"Content-Type": "application/json"}, timeout=600)
    if status != 409:
        resume.pop(filepath, None)  # done, refused or discarded (422): nothing to resume
    return status, body


IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.jxl', '.gif', '.apng'}
VIDEO_EXTENSIONS = {'.mp4', '.webm', '.mkv', '.mov', '.avi', '.m4v', '.mpg',
                    '.mpeg', '.wmv', '.flv', '.ts', '.ogv'}
AUDIO_EXTENSIONS = {'.mp3', '.flac', '.aac', '.ogg', '.oga', '.opus',
                    '.wav', '.wma', '.aiff', '.aif'}
RAW_EXTENSIONS = {
    '.dng', '.cr2', '.cr3', '.crw', '.nef', '.nrw', '.arw', '.srf', '.sr2',
    '.raf', '.rw2', '.orf', '.pef', '.ptx', '.raw', '.rwl', '.iiq', '.3fr',
    '.fff', '.mef', '.mos', '.mrw', '.x3f', '.erf', '.kdc', '.dcr',
}
BOOK_EXTENSIONS = {
    '.epub', '.mobi', '.azw', '.azw3', '.kf8', '.kfx', '.lit', '.fb2',
    '.lrf', '.lrx', '.chm', '.ceb', '.docx', '.rtf', '.pdf',
    '.cbz', '.cbr', '.cb7', '.cbt', '.cba',
}
MEDIA_EXTENSIONS = (IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | RAW_EXTENSIONS
                    | AUDIO_EXTENSIONS | BOOK_EXTENSIONS)

NON_MEDIA_EXTENSIONS = {'.txt', '.xmp', '.json', '.md', '.ini', '.log', '.db'}

PERMANENT_ERROR_CODES = {
    "exact_duplicate",
    "filename_exists",
    "bad_folder",
    "no_file",
    "conversion_failed",
}
TEMPORARY_ERROR_CODES = {
    "server_error",
}
AUTH_STATUS_CODES = {401, 403}


class Outcome(Enum):
    SUCCESS   = "success"
    QUEUED    = "queued"  # spooled by the server, no verdict yet
    DUPLICATE = "duplicate"  # exact_duplicate or filename_exists
    SKIPPED   = "skipped"  # other permanent rejection
    FAILED    = "failed"  # gave up after retries


class UploadResult(object):
    """! @brief One file's outcome."""

    def __init__(self, filepath, outcome, message, error_code=None, existing_file=None,
                 attempts=1, size=0, chunked=False):
        self.filepath = filepath
        self.outcome = outcome
        self.message = message
        self.error_code = error_code
        self.existing_file = existing_file
        self.attempts = attempts
        self.size = size
        self.chunked = chunked


def load_classes(source_dir):
    p = os.path.join(source_dir, "classes.txt")
    if os.path.exists(p):
        with open(p, encoding='utf-8') as f:
            return [l.strip() for l in f if l.strip()]
    return []


def parse_sidecar(filepath, classes_map):
    """! @brief Read a file's .txt sidecar: "tag|tag|description: text", YOLO label
    lines, or plain text as the description.
    @return (regions, description, tags).
    """
    sidecar = os.path.splitext(filepath)[0] + ".txt"
    # A .txt book would be its own sidecar.
    if os.path.abspath(sidecar) == os.path.abspath(filepath):
        return [], "", []
    # Books carry their own metadata; a .txt beside one is a stray note.
    if os.path.splitext(filepath)[1].lower() in BOOK_EXTENSIONS:
        return [], "", []
    if not os.path.exists(sidecar):
        return [], "", []
    try:
        with open(sidecar, encoding='utf-8') as fh:
            content = fh.read().strip()
    except Exception as e:
        print("  [!] Could not read sidecar {}: {}".format(sidecar, e))
        return [], "", []
    if not content:
        return [], "", []

    # pipe-separated tags
    if content.count('|') > 1:
        tags, desc_parts = [], []
        for t in [x.strip() for x in content.split('|') if x.strip()]:
            tl = t.lower()
            if tl.startswith('description:'):
                clean = t[12:].strip()
                if clean:
                    desc_parts.append(clean)
            elif len(t) > 20:
                desc_parts.append(t)
            else:
                tags.append(t)
        return [], "; ".join(desc_parts), tags

    # YOLO labels
    lines = [l.strip() for l in content.split('\n') if l.strip()]
    regions = []
    is_yolo = True
    for line in lines:
        parts = line.split()
        if len(parts) != 5:
            is_yolo = False
            break
        try:
            cid = int(parts[0])
            cx, cy, w, h = map(float, parts[1:])
            if not all(0.0 <= v <= 1.0 for v in (cx, cy, w, h)):
                is_yolo = False
                break
            name = classes_map[cid] if cid < len(classes_map) else "class_{}".format(cid)
            regions.append({"class_name": name, "cx": cx, "cy": cy, "w": w, "h": h})
        except ValueError:
            is_yolo = False
            break
    if is_yolo and regions:
        return regions, "", []

    # plain description
    return [], content, []


def _post_streaming(session, filepath, fname, form_data, timeout, sha=None):
    """! @brief POST a file to /api/upload as a streamed multipart body.
    @return (status, decoded JSON).
    """
    first = StreamingMultipart(form_data, "file", filepath, fname)

    def body():  # a fresh stream per send (a re-login resends), same boundary
        return StreamingMultipart(form_data, "file", filepath, fname, first.boundary)
    headers = first.headers()
    if sha:
        headers["X-Content-SHA256"] = sha
    return session.request("POST", "/api/upload", body=body, headers=headers, timeout=timeout)


def _use_chunked(session, size, chunked):
    """! @brief Whether a file goes through an upload session.
    @param chunked  "on" (always), "off" (never) or "auto" (big files, when offered).
    """
    if chunked == "off":
        return False
    if chunked == "on":
        return True  # a server without sessions answers 404: single request then
    return bool(session.config.get("sessions")) and \
        size > int(session.config.get("chunked_over") or CHUNKED_OVER)


def upload_file(filepath, source_dir, classes_map, endpoint, max_attempts,
                initial_backoff, session, dest="", mode="auto", chunked="auto",
                validate=False, resume=None):
    """! @brief Upload one file with retries. @return an UploadResult."""
    rel_dir = os.path.relpath(os.path.dirname(filepath), source_dir)
    parts   = [p for p in (dest, rel_dir if rel_dir != "." else "") if p]
    folder  = "/".join(parts).replace('\\', '/')
    fname   = os.path.basename(filepath)
    size    = os.path.getsize(filepath)
    use_chunks = _use_chunked(session, size, chunked)
    resume = resume if resume is not None else {}

    regions, description, tags = parse_sidecar(filepath, classes_map)
    metadata = {}
    if regions or description or tags:
        metadata = {"tags": tags, "description": description, "regions": regions}

    last_error  = ""
    last_code   = ""
    last_detail = ""
    sha = None

    def result(outcome, message, attempt, **kw):
        return UploadResult(filepath, outcome, message, attempts=attempt, size=size,
                            chunked=use_chunks, **kw)

    for attempt in range(1, max_attempts + 1):
        try:
            got = None
            if use_chunks:
                got = chunked_upload(session, filepath, fname, folder, metadata, mode,
                                     validate=validate, backoff=initial_backoff, resume=resume)
                if got is None:
                    use_chunks = False  # no sessions on this server
            if got is None:
                if validate and sha is None:
                    sha = file_sha256(filepath)
                form_data = {'folder': folder, 'mode': mode}
                if metadata:
                    form_data['metadata'] = json.dumps(metadata)
                got = _post_streaming(session, filepath, fname, form_data, timeout=180,
                                      sha=sha if validate else None)
            status, body = got

            if status == 200 and body.get('success'):
                # The server reports a pre-existing file as a duplicate on a 200.
                if body.get('duplicate'):
                    existing = body.get('existing_file') or body.get('filename')
                    return result(Outcome.DUPLICATE,
                                  "duplicate of {}".format(existing) if existing else "duplicate",
                                  attempt, error_code=body.get('error_code'),
                                  existing_file=existing)
                # The server fixed a wrong extension: report it.
                corrected = body.get('corrected_extension') or {}
                note = ""
                if corrected:
                    note = "  [type corrected {} -> {}]".format(
                        corrected.get('from') or '(none)', corrected.get('to'))
                return result(Outcome.SUCCESS,
                              "-> {}{}".format(body.get('filename', fname), note), attempt)

            if status == 202 and body.get('success'):
                if mode == 'sync' and attempt < max_attempts:
                    last_error = "server queued instead of confirming inline"
                    last_code  = "unexpected_queue"
                    time.sleep(initial_backoff * (2 ** (attempt - 1)))
                    continue
                return result(Outcome.QUEUED, "queued -> {} (queue_id {})".format(
                    body.get('filename', fname), body.get('queue_id', '?')), attempt)

            error_code = body.get('error_code', '')
            error_msg  = body.get('error', "HTTP {}".format(status))
            detail     = body.get('detail', '')
            existing   = body.get('existing_file')

            # Expired or revoked session that a re-login could not fix.
            if status in AUTH_STATUS_CODES and session.auth_enabled:
                return result(Outcome.FAILED, "authentication failed and could not be renewed",
                              attempt, error_code="auth_failed")

            if error_code in ('exact_duplicate', 'filename_exists'):
                return result(Outcome.DUPLICATE,
                              "duplicate of {}".format(existing) if existing else error_msg,
                              attempt, error_code=error_code, existing_file=existing)

            # the bytes arrived damaged and were discarded: send again
            if error_code == "checksum_mismatch":
                last_error, last_code = error_msg, error_code
            # permanent: no retry
            elif error_code in PERMANENT_ERROR_CODES or (
                    400 <= status < 500 and status not in (408, 409)):
                msg = error_msg
                if detail:
                    msg += " ({})".format(detail)
                return result(Outcome.SKIPPED, msg, attempt, error_code=error_code)
            else:
                # temporary: retry
                last_error  = error_msg
                last_code   = error_code
                last_detail = detail

        except TransportError as e:
            last_error = "connection error: {}".format(e)
            last_code  = "connection_error"
        except Exception as e:
            last_error = str(e)
            last_code  = "client_error"

        if attempt < max_attempts:
            time.sleep(initial_backoff * (2 ** (attempt - 1)))

    msg = "gave up after {} attempt(s): {}".format(max_attempts, last_error)
    if last_detail:
        msg += " ({})".format(last_detail)
    return result(Outcome.FAILED, msg, max_attempts, error_code=last_code)


def _human(n):
    """! @brief A byte count for people (1.5 MB)."""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "{:.1f} {}".format(n, unit) if unit != "B" else "{} B".format(int(n))
        n /= 1024.0
    return "{} B".format(int(n))


def print_summary(results, verbose_duplicates, elapsed=None):
    by_outcome = dict((o, []) for o in Outcome)
    for r in results:
        by_outcome[r.outcome].append(r)

    total     = len(results)
    succeeded = len(by_outcome[Outcome.SUCCESS])
    queued    = len(by_outcome[Outcome.QUEUED])
    dupes     = len(by_outcome[Outcome.DUPLICATE])
    skipped   = len(by_outcome[Outcome.SKIPPED])
    failed    = len(by_outcome[Outcome.FAILED])
    sent      = sum(r.size for r in results if r.outcome in (Outcome.SUCCESS, Outcome.QUEUED))
    chunked   = sum(1 for r in results if r.chunked)

    print("\n" + "-" * 60)
    print("  Total:      {}".format(total))
    print("  Uploaded:   {}".format(succeeded))
    if queued:
        print("  Queued:     {}  (spooled on server; converts later)".format(queued))
    print("  Duplicates: {}  (skipped - already on server)".format(dupes))
    print("  Skipped:    {}  (permanent rejection)".format(skipped))
    print("  Failed:     {}  (gave up after retries)".format(failed))
    if chunked:
        print("  Chunked:    {}  (resumable sessions)".format(chunked))
    if elapsed:
        rate = sent / elapsed if elapsed > 0 else 0
        print("  Sent:       {} in {:.1f}s ({}/s)".format(_human(sent), elapsed, _human(rate)))
    print("-" * 60)

    if verbose_duplicates and by_outcome[Outcome.DUPLICATE]:
        print("\nDuplicate files:")
        for r in by_outcome[Outcome.DUPLICATE]:
            fname = os.path.basename(r.filepath)
            if r.existing_file:
                print("  {}  ->  exists as  {}".format(fname, r.existing_file))
            else:
                print("  {}  (filename conflict)".format(fname))

    if by_outcome[Outcome.SKIPPED]:
        print("\nPermanently rejected files:")
        for r in by_outcome[Outcome.SKIPPED]:
            print("  {}: [{}] {}".format(os.path.basename(r.filepath), r.error_code, r.message))

    if by_outcome[Outcome.FAILED]:
        print("\nFiles that failed after all retries:")
        for r in by_outcome[Outcome.FAILED]:
            print("  {}: {}".format(os.path.basename(r.filepath), r.message))


def _should_upload(fname, aggressive):
    ext = os.path.splitext(fname)[1].lower()
    if aggressive:
        # Anything that isn't a sidecar goes up; the server rejects what it can't convert.
        if fname == "classes.txt":
            return False
        return ext not in NON_MEDIA_EXTENSIONS
    return ext in MEDIA_EXTENSIONS


def bulk_upload(source_dir, server_url, workers, max_attempts, initial_backoff,
                verbose_dupes, aggressive=False, username="", password="",
                verify_tls=True, dest="", mode="auto", chunked="auto", validate=False):
    """! @brief Upload a folder tree. @return the exit code."""
    source_dir = os.path.abspath(source_dir)
    if not os.path.isdir(source_dir):
        print("Error: '{}' is not a directory.".format(source_dir))
        return 2

    dest = "/".join(
        s for s in dest.replace('\\', '/').split('/')
        if s and s not in ('.', '..')
    )

    session = Session(server_url, username, password, verify=verify_tls)
    try:
        cfg = session.probe()
    except AuthError as e:
        print("Error: {}".format(e))
        return 2

    if session.auth_enabled:
        if cfg.get("needs_bootstrap"):
            print("[*] Server has no users yet - this login will create the "
                  "initial admin account.")
        if not session.username:
            print("Error: server requires authentication. Pass --username "
                  "(and --password, or set CIM_PASSWORD, or be prompted).")
            return 2
        try:
            session.login()
        except AuthError as e:
            print("Error: {}".format(e))
            return 2
        who = (session.user or {}).get("username", session.username)
        admin = " (admin)" if (session.user or {}).get("is_admin") else ""
        print("[*] Authenticated as {}{} (mode: {}).".format(who, admin, cfg.get('mode', '?')))
    elif username:
        # Credentials the server doesn't want usually mean a wrong --url.
        print("[*] Server has authentication disabled; ignoring --username.")

    offer = session.server_config()
    if offer.get("validate") and not validate:
        validate = True
        print("[*] The server asks for verified uploads: sending sha256 checksums.")

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

    endpoint = "{}/api/upload".format(server_url.rstrip('/'))
    total    = len(files)
    print("[*] Found {} file(s).  Server: {}".format(total, endpoint))
    _mode_desc = {"sync":  "sync (inline convert; true receipt per file)",
                  "spool": "spool (server queues; converts later)",
                  "auto":  "auto (inline while the server keeps up, else spool)"}
    print("[*] Ingest mode: {}".format(_mode_desc.get(mode, mode)))
    if offer.get("sessions"):
        print("[*] Chunked sessions: {}".format(
            {"on": "every file", "off": "never"}.get(chunked, "files over {}".format(
                _human(offer.get("chunked_over") or CHUNKED_OVER)))))
    if max_attempts > 1:
        print("[*] Retries: up to {} attempts, {}s initial backoff (exponential).\n".format(
            max_attempts, initial_backoff))
    else:
        print()

    results = []
    completed = 0
    resume = {}  # filepath -> session id, so a retry continues a chunked upload
    started = time.time()

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = dict(
            (ex.submit(upload_file, fp, source_dir, classes_map, endpoint, max_attempts,
                       initial_backoff, session, dest, mode, chunked, validate, resume), fp)
            for fp in files)
        for future in as_completed(futures):
            completed += 1
            r = future.result()
            results.append(r)
            icon = {"success": "+", "queued": "...", "duplicate": "=", "skipped": "!",
                    "failed": "x"}[r.outcome.value]
            fname = os.path.basename(r.filepath)
            atts  = " (attempt {})".format(r.attempts) if r.attempts > 1 else ""
            print("  [{:>{w}}/{}] {} {}{}  {}".format(completed, total, icon, fname, atts,
                                                     r.message, w=len(str(total))))

    session.logout()

    print_summary(results, verbose_dupes, elapsed=time.time() - started)

    failed_count = sum(1 for r in results if r.outcome == Outcome.FAILED)
    if failed_count == total:
        return 2
    if failed_count > 0:
        return 1
    return 0


def main():
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
    parser.add_argument("--mode", choices=("auto", "sync", "spool"),
        default="auto",
        help="Ingest mode.")
    parser.add_argument("--verbose-duplicates", action="store_true",
        help="List every duplicate with its existing server path in the summary.")
    parser.add_argument("--aggressive", action="store_true",
        help="Upload every file (except sidecars), including ones with a wrong "
             "or missing extension, and let the server try to convert them. "
             "Useful for recovering misnamed media.")
    chunk_group = parser.add_mutually_exclusive_group()
    chunk_group.add_argument("--chunked", dest="chunked", action="store_const", const="on",
        default="auto",
        help="Send every file through resumable chunked sessions (default: only files "
             "over 8 MB, when the server offers sessions; falls back to a single "
             "request on a server without them).")
    chunk_group.add_argument("--no-chunked", dest="chunked", action="store_const", const="off",
        help="Never use chunked sessions.")
    parser.add_argument("--validate", action="store_true",
        help="Send each file's sha256; the server refuses damaged bytes and the file "
             "is sent again (on automatically when the server asks for it).")

    auth_group = parser.add_argument_group("authentication")
    auth_group.add_argument("--username", default=os.environ.get("CIM_USERNAME", ""),
        help="Username for servers with authentication enabled. "
             "Defaults to $CIM_USERNAME.")
    auth_group.add_argument("--password", default=None,
        help="Password. Prefer $CIM_PASSWORD or the interactive prompt - a "
             "password passed here is visible in ps output and shell history.")
    auth_group.add_argument("--password-file", default=None,
        help="Read the password from this file (first line). Safer than "
             "--password for scripts and cron jobs.")
    auth_group.add_argument("--no-verify-tls", action="store_true",
        help="Skip TLS certificate verification (self-signed https servers).")
    args = parser.parse_args()

    # Password: --password-file, then $CIM_PASSWORD, then --password, then a prompt
    # (only with a tty, so cron never hangs).
    password = ""
    if args.password_file:
        try:
            with open(args.password_file, "r", encoding="utf-8") as fh:
                password = fh.readline().strip()
        except OSError as e:
            print("Error: cannot read --password-file: {}".format(e))
            sys.exit(2)
    elif os.environ.get("CIM_PASSWORD"):
        password = os.environ["CIM_PASSWORD"]
    elif args.password is not None:
        password = args.password
    elif args.username and sys.stdin.isatty():
        try:
            password = getpass.getpass("Password for {}: ".format(args.username))
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(2)

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
        mode            = args.mode,
        chunked         = args.chunked,
        validate        = args.validate,
    ))


if __name__ == "__main__":
    main()
