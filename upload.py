"""! @file
@brief Command-line bulk uploader.

Exit codes: 0 all uploaded (or expected duplicates), 1 some failed after
retries, 2 all failed (connection or configuration).
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

# same as auth.COOKIE_NAME on the server
COOKIE_NAME = "cim_session"

# Stream uploads from disk: requests' files= reads a whole file into memory
# (a 13 GB video would). requests-toolbelt's encoder, else the fallback below.
try:
    from requests_toolbelt.multipart.encoder import MultipartEncoder  # type: ignore
    _HAVE_TOOLBELT = True
except Exception:  # pragma: no cover
    MultipartEncoder = None  # type: ignore
    _HAVE_TOOLBELT = False

class AuthError(Exception):
    """! @brief The uploader could not get or refresh a server session."""

class Session:
    """! @brief The uploader's authenticated connection: session cookie plus the CSRF
    token every POST must echo.

    One requests.Session is shared by all workers; login and re-login happen under
    a lock. A 401 mid-run triggers one re-login and a retry. With auth off on the
    server, no credentials are sent.
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
        self._lock = threading.Lock()
        # Bumped per login, so a 401 from a request sent before someone else's
        # re-login just retries.
        self._generation = 0

    def probe(self) -> dict:
        """! @brief Ask whether the server has auth on (an old server without the endpoint counts as off)."""
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

    def login(self) -> None:
        """! @brief Log in; store the session cookie and CSRF token."""
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
                log_error(f"re-authentication failed: {e}")
                return False

    @property
    def generation(self) -> int:
        return self._generation

    def headers(self) -> dict:
        """! @brief Headers for a POST (CSRF when signed in)."""
        return {"X-CSRF-Token": self.csrf} if self.csrf else {}

    def logout(self) -> None:
        """! @brief End the server-side session (best effort)."""
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
    """! @brief Fallback streaming multipart body: preamble, the file in 1 MiB chunks, epilogue."""

    _CHUNK = 1024 * 1024

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
    """! @brief POST a file as a streamed multipart body, with the session cookie and CSRF header."""
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

@dataclass
class UploadResult:
    filepath:      str
    outcome:       Outcome
    message:       str
    error_code:    Optional[str] = None
    existing_file: Optional[str] = None
    attempts:      int = 1


def load_classes(source_dir: str) -> list[str]:
    p = os.path.join(source_dir, "classes.txt")
    if os.path.exists(p):
        with open(p, encoding='utf-8') as f:
            return [l.strip() for l in f if l.strip()]
    return []

def parse_sidecar(filepath: str, classes_map: list[str]) -> tuple:
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
        content = open(sidecar, encoding='utf-8').read().strip()
    except Exception as e:
        print(f"  [!] Could not read sidecar {sidecar}: {e}")
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
                if clean: desc_parts.append(clean)
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

    # plain description
    return [], content, []


def upload_file(
    filepath:        str,
    source_dir:      str,
    classes_map:     list[str],
    endpoint:        str,
    max_attempts:    int,
    initial_backoff: float,
    session:         "Session",
    dest:            str = "",
    mode:            str = "auto",
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
            form_data = {'folder': folder, 'mode': mode}
            if metadata:
                form_data['metadata'] = json.dumps(metadata)
            # Remember the session generation before sending, to tell a stale 401 apart.
            gen = session.generation
            resp = _post_streaming(session, endpoint, filepath, fname,
                                   form_data, timeout=180)

            try:
                body = resp.json()
            except Exception:
                body = {}

            if resp.status_code == 200 and body.get('success'):
                # The server reports a pre-existing file as a duplicate on a 200.
                if body.get('duplicate'):
                    existing = body.get('existing_file') or body.get('filename')
                    return UploadResult(
                        filepath=filepath, outcome=Outcome.DUPLICATE,
                        message=f"duplicate of {existing}" if existing
                                else "duplicate",
                        error_code=body.get('error_code'),
                        existing_file=existing, attempts=attempt)
                # The server fixed a wrong extension: report it.
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

            if resp.status_code == 202 and body.get('success'):
                if mode == 'sync' and attempt < max_attempts:
                    last_error = "server queued instead of confirming inline"
                    last_code  = "unexpected_queue"
                    backoff = initial_backoff * (2 ** (attempt - 1))
                    time.sleep(backoff)
                    continue
                return UploadResult(
                    filepath=filepath,
                    outcome=Outcome.QUEUED,
                    message=f"queued → {body.get('filename', fname)} "
                            f"(queue_id {body.get('queue_id','?')})",
                    attempts=attempt,
                )

            error_code = body.get('error_code', '')
            error_msg  = body.get('error', f"HTTP {resp.status_code}")
            detail     = body.get('detail', '')
            existing   = body.get('existing_file')

            # Expired or revoked session: log in again and retry. Checked before the
            # generic 4xx branch, which would drop the file as permanently skipped.
            if resp.status_code in AUTH_STATUS_CODES and session.auth_enabled:
                if session.relogin(gen):
                    last_error = f"session expired; re-authenticated ({error_msg})"
                    last_code  = "auth_retry"
                    # no backoff: a credential refresh, not server load
                    continue
                return UploadResult(
                    filepath=filepath,
                    outcome=Outcome.FAILED,
                    message="authentication failed and could not be renewed",
                    error_code="auth_failed",
                    attempts=attempt,
                )

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

            # permanent: no retry
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

            # temporary: retry
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

        if attempt < max_attempts:
            backoff = initial_backoff * (2 ** (attempt - 1))
            time.sleep(backoff)

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


def print_summary(results: list[UploadResult], verbose_duplicates: bool) -> None:
    by_outcome: dict[Outcome, list[UploadResult]] = {o: [] for o in Outcome}
    for r in results:
        by_outcome[r.outcome].append(r)

    total     = len(results)
    succeeded = len(by_outcome[Outcome.SUCCESS])
    queued    = len(by_outcome[Outcome.QUEUED])
    dupes     = len(by_outcome[Outcome.DUPLICATE])
    skipped   = len(by_outcome[Outcome.SKIPPED])
    failed    = len(by_outcome[Outcome.FAILED])

    print("\n" + "-" * 60)
    print(f"  Total:      {total}")
    print(f"  Uploaded:   {succeeded}")
    if queued:
        print(f"  Queued:     {queued}  (spooled on server; converts later)")
    print(f"  Duplicates: {dupes}  (skipped - already on server)")
    print(f"  Skipped:    {skipped}  (permanent rejection)")
    print(f"  Failed:     {failed}  (gave up after retries)")
    print("-" * 60)

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


def _should_upload(fname: str, aggressive: bool) -> bool:
    ext = os.path.splitext(fname)[1].lower()
    if aggressive:
        # Anything that isn't a sidecar goes up; the server rejects what it can't convert.
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
    mode:            str = "auto",
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
            print("[*] Server has no users yet - this login will create the "
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
        # Credentials the server doesn't want usually mean a wrong --url.
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
    _mode_desc = {"sync":  "sync (inline convert; true receipt per file)",
                  "spool": "spool (server queues; converts later)",
                  "auto":  "auto (inline while the server keeps up, else spool)"}
    print(f"[*] Ingest mode: {_mode_desc.get(mode, mode)}")
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
                endpoint, max_attempts, initial_backoff, session, dest, mode
            ): fp
            for fp in files
        }
        for future in as_completed(futures):
            completed += 1
            r = future.result()
            results.append(r)

            icon = {"success":"✓","queued":"...","duplicate":"=","skipped":"!","failed":"✗"}[r.outcome.value]
            fname = os.path.basename(r.filepath)
            atts  = f" (attempt {r.attempts})" if r.attempts > 1 else ""
            print(f"  [{completed:>{len(str(total))}}/{total}] {icon} {fname}{atts}  {r.message}")

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
    parser.add_argument("--mode", choices=("auto", "sync", "spool"),
        default="auto",
        help="Ingest mode.")
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
        # the user chose --insecure
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
        mode            = args.mode,
    ))

if __name__ == "__main__":
    main()