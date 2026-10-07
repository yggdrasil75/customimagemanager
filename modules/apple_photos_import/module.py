"""! @file
@brief Apple Photos importer - two ways in, both fetch-module fetchers.
======================================================================
iCloud Photos, LIVE (fetcher "icloud"). Signs in to the iCloud web API
(pyicloud - the route icloudpd uses; no Mac or iPhone app needed), pulls
originals with their dates, favourites, hidden state, albums and live-photo
videos, and with a schedule keeps pulling what's new. Optionally PURGES from
iCloud what is safely here and older than a keep window, so the phone can
take photos, CIM keeps them, and iCloud storage stays small (deleted items sit
in Recently Deleted for 30 days). Needs two-factor authentication and
"Access iCloud Data on the Web" on. The Apple ID password is used once to sign
in and never stored; the session lasts about two months, then the tab asks to
sign in again.

Apple's data EXPORT (fetcher "apple_export"): privacy.apple.com -> "Request a
copy of your data" -> iCloud Photos. Zips of originals plus CSVs (favourite,
hidden, deleted, creation date, albums); see icloud.py. A plain folder of
originals (icloudpd output) works too.
"""

import hashlib
import os
import threading
import time

from modules.fetch.importing import Importer, deliver, map_meta, resolve_in_root, run_export_folder

from . import icloud, icloud_sync

MANIFEST = {
    "id":          "apple_photos_import",
    "name":        "Apple Photos import",
    "version":     "1.1.0",
    "description": "Import from iCloud Photos - live and periodic via the iCloud web API, with optional purge "
                   "from iCloud once safely imported - or from Apple's data export.",
    "core":        False,
    "requires":    ["fetch"],
    "pip":         ["pyicloud", "rich"],
    "assets":      ["apple_photos_import.js"],
}


def _pyicloud():
    try:
        from pyicloud import PyiCloudService
        from pyicloud import exceptions as E
        return PyiCloudService, E
    except Exception:
        return None, None


def register(host):
    fetch = host.get_service("fetch")
    host.add_settings_tab("icloud_import", "iCloud Photos", icon="\U0001f4e5", admin_only=True)
    host.add_settings_tab("apple_export_import", "Apple export import", icon="\U0001f4e5", admin_only=True)
    host.add_config_key("icloud_session_dir", default="data/icloud",
                        validate=lambda v: str(v or "data/icloud").strip() or "data/icloud")
    _pending = {}                     # apple id -> signed-in-but-unverified session (awaiting the 2FA code)
    _lock = threading.Lock()

    def cookie_dir(apple_id):
        d = os.path.abspath(os.path.join(str(host.config.get("icloud_session_dir") or "data/icloud"),
                                         hashlib.sha1(apple_id.lower().encode()).hexdigest()[:16]))
        os.makedirs(d, mode=0o700, exist_ok=True)
        return d

    def _connect(apple_id, password=None):
        Service, E = _pyicloud()
        if Service is None:
            raise ValueError("iCloud access needs pyicloud (pip install pyicloud rich)")
        try:
            return Service(apple_id, password or None, cookie_directory=cookie_dir(apple_id))
        except E.PyiCloudFailedLoginException:
            raise ValueError("Apple rejected the Apple ID or password")
        except (E.PyiCloudNoStoredPasswordAvailableException, E.PyiCloudAuthRequiredException,
                E.PyiCloudPasswordException):
            raise ValueError("sign-in needed: enter your Apple ID password")
        except E.PyiCloud2SARequiredException:
            raise ValueError("this Apple ID uses the old two-step verification; switch it to two-factor authentication")
        except E.PyiCloudServiceNotActivatedException:
            raise ValueError("iCloud isn't set up on this Apple ID")
        except E.PyiCloudAcceptTermsException:
            raise ValueError("Apple wants new terms accepted: sign in at icloud.com once, then try again")
        except E.PyiCloudException as e:
            raise ValueError(f"iCloud: {e} (with Advanced Data Protection, turn on "
                             f"'Access iCloud Data on the Web' in Settings → Apple ID → iCloud)")

    # -- iCloud (live) ------------------------------------------------------
    def _validate_icloud(cfg, secrets, sid):
        apple_id = str(cfg.get("apple_id") or "").strip()
        if not apple_id:
            raise ValueError("Apple ID is required")
        cfg["apple_id"] = apple_id
        password = secrets.pop("password", "")      # used to sign in, never stored
        if cfg.get("purge") and float(cfg.get("keep_days") or 0) < 1:
            raise ValueError("with purge on, keep at least 1 day of photos in iCloud")
        api = _connect(apple_id, password)
        prompt = None
        if getattr(api, "requires_2fa", False):
            with _lock:
                _pending[apple_id.lower()] = api
            prompt = {"action": "code", "field": "code", "label": "Verification code", "button": "Verify",
                      "message": "Apple sent a 6-digit code to your trusted devices."}
        elif getattr(api, "requires_2sa", False):
            raise ValueError("this Apple ID uses the old two-step verification; switch it to two-factor authentication")
        return cfg, secrets, f"iCloud: {apple_id}", prompt

    def _action_code(src, body):
        apple_id = src["config"]["apple_id"]
        with _lock:
            api = _pending.get(apple_id.lower())
        if api is None:
            raise ValueError("the sign-in timed out (or the server restarted): save again with your password")
        code = str(body.get("code") or "").strip()
        if not api.validate_2fa_code(code):
            raise ValueError("Apple didn't accept that code")
        if not getattr(api, "is_trusted_session", True):
            api.trust_session()
        with _lock:
            _pending.pop(apple_id.lower(), None)
        return {"signed_in": True, "message": "Signed in to iCloud. The session lasts about two months."}

    icloud_imp = Importer(host, "icloud", validate=_validate_icloud, actions={"code": _action_code},
                          default_folder="icloud/{year}")

    def _fetch_icloud(target, tmpdir, on_file, ctx=None):
        src = icloud_imp.source(target)
        if src is None:
            raise RuntimeError("this iCloud source was removed")
        cfg = src["config"]
        ctx.scope = f"{src['id']}:{cfg['apple_id'].lower()}"
        try:
            api = _connect(cfg["apple_id"])
            if getattr(api, "requires_2fa", False) or getattr(api, "requires_2sa", False):
                raise ValueError("sign-in needed")
        except ValueError as e:
            icloud_imp.update_source(src["id"], status="needs sign-in")
            raise RuntimeError(f"iCloud session expired or signed out ({e}) - sign in again from the iCloud tab")
        delivered = []

        def collect(it_iter):
            for it in it_iter:
                delivered.append(it)
                yield it

        yield from deliver(ctx, collect(icloud_sync.items(api, cfg, ctx.message)), tmpdir, on_file, cfg)
        if ctx.stopping() or not cfg.get("purge"):
            return
        fetch.reconcile()                          # count what finished ingesting during this run
        n = icloud_sync.purge(ctx, host, delivered, cfg)
        if n:
            ctx.message(f"removed {n} photo(s) from iCloud that are safely here "
                        f"(in iCloud's Recently Deleted for 30 days)")

    fetch.register({"id": "icloud", "label": "iCloud Photos", "available": lambda: _pyicloud()[0] is not None,
                    "handles": lambda t: str(t).startswith("icloud:"), "target_key": lambda t: t,
                    "fetch": _fetch_icloud, "map_meta": map_meta})

    # -- Apple data export --------------------------------------------------
    def _validate_export(cfg, secrets, sid):
        if not cfg.get("path"):
            raise ValueError("choose the export zip or the folder it's in")
        resolve_in_root(host, cfg["path"])
        return cfg, secrets, f"Apple export: {cfg['path']}", None

    export_imp = Importer(host, "apple_export", validate=_validate_export, default_folder="apple-photos/{year}",
                          file_source=True)

    def _fetch_export(target, tmpdir, on_file, ctx=None):
        src = export_imp.source(target)
        if src is None:
            raise RuntimeError("this export source was removed")
        ctx.scope = f"{src['id']}:export"
        yield from run_export_folder(ctx, export_imp, src, tmpdir, on_file, icloud.scan)

    fetch.register({"id": "apple_export", "label": "Apple data export", "available": lambda: True,
                    "handles": lambda t: str(t).startswith("apple_export:"), "target_key": lambda t: t,
                    "fetch": _fetch_export, "map_meta": map_meta})
    host.add_asset("apple_photos_import.js")
    host.provide_service("apple_photos_import", {"connect": _connect, "pending": _pending})
