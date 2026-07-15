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
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import requests


# ── Constants ─────────────────────────────────────────────────────────────────

# Media the server will accept: still images + gifs (server converts to jxl,
# animated gifs → animated jxl) and video files (stored natively).
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.jxl', '.gif', '.apng'}
VIDEO_EXTENSIONS = {'.mp4', '.webm', '.mkv', '.mov', '.avi', '.m4v', '.mpg',
                    '.mpeg', '.wmv', '.flv', '.ts', '.ogv'}
# Camera raws: the server develops these into jxl on upload (via rawpy).
RAW_EXTENSIONS = {
    '.dng', '.cr2', '.cr3', '.crw', '.nef', '.nrw', '.arw', '.srf', '.sr2',
    '.raf', '.rw2', '.orf', '.pef', '.ptx', '.raw', '.rwl', '.iiq', '.3fr',
    '.fff', '.mef', '.mos', '.mrw', '.x3f', '.erf', '.kdc', '.dcr',
}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | RAW_EXTENSIONS

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
) -> UploadResult:
    rel_dir = os.path.relpath(os.path.dirname(filepath), source_dir)
    folder  = rel_dir.replace('\\', '/') if rel_dir != "." else ""
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
            with open(filepath, 'rb') as fh:
                form_data = {'folder': folder}
                if metadata:
                    form_data['metadata'] = json.dumps(metadata)
                resp = requests.post(
                    endpoint,
                    files={'file': (fname, fh)},
                    data=form_data,
                    timeout=180,
                )

            # Parse response
            try:
                body = resp.json()
            except Exception:
                body = {}

            if resp.status_code == 200 and body.get('success'):
                return UploadResult(
                    filepath=filepath,
                    outcome=Outcome.SUCCESS,
                    message=f"→ {body.get('filename', fname)}",
                    attempts=attempt,
                )

            error_code = body.get('error_code', '')
            error_msg  = body.get('error', f"HTTP {resp.status_code}")
            detail     = body.get('detail', '')
            existing   = body.get('existing_file')

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

def bulk_upload(
    source_dir:      str,
    server_url:      str,
    workers:         int,
    max_attempts:    int,
    initial_backoff: float,
    verbose_dupes:   bool,
) -> int:
    source_dir = os.path.abspath(source_dir)
    if not os.path.isdir(source_dir):
        print(f"Error: '{source_dir}' is not a directory.")
        return 2

    classes_map = load_classes(source_dir)
    files = [
        os.path.join(root, f)
        for root, _, filenames in os.walk(source_dir)
        for f in filenames
        if os.path.splitext(f)[1].lower() in MEDIA_EXTENSIONS
    ]
    if not files:
        print("No media files found.")
        return 0

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
                endpoint, max_attempts, initial_backoff
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
    parser.add_argument("--workers", type=int, default=8,
        help="Number of concurrent uploads.")
    parser.add_argument("--retries", type=int, default=3,
        help="Max attempts per file for transient errors (1 = no retry).")
    parser.add_argument("--backoff", type=float, default=2.0,
        help="Initial retry backoff in seconds (doubles each attempt).")
    parser.add_argument("--verbose-duplicates", action="store_true",
        help="List every duplicate with its existing server path in the summary.")
    args = parser.parse_args()

    sys.exit(bulk_upload(
        source_dir      = args.source_dir,
        server_url      = args.url,
        workers         = args.workers,
        max_attempts    = args.retries,
        initial_backoff = args.backoff,
        verbose_dupes   = args.verbose_duplicates,
    ))


if __name__ == "__main__":
    main()