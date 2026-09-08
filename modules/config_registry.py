"""
Config registry.
======================================================================
One place that knows what settings exist, instead of that knowledge being
smeared across manager.py's state dict, save_config allowlist, and the
update_settings if-ladder. Core AND modules declare their settings here;
the registry owns defaults, persistence, validation, and change handlers.

A declared setting has:
    key       stable name (what the front end sends, what's stored)
    default   value used when absent
    save      whether it's persisted to app_config.json (default True)
    validate  optional fn(value) -> cleaned value (raise/return None to reject)
    on_change optional fn(new, old) run when update_settings changes it
    owner     module id that declared it ('core' for built-ins)

update_settings no longer needs a branch per key: it validates each
incoming key against the registry, stores it, and fires its handler. A
module owning a setting (e.g. rating owning 'iqa_model' -> broker.select)
registers the key + on_change and never touches manager.

This does NOT replace the live `state` dict — state stays the runtime
store everything reads. The registry seeds state's defaults, filters what
save persists, and routes writes. Core settings can migrate onto it
incrementally; anything not yet declared still works the old way.
"""

import threading


class ConfigRegistry:
    def __init__(self):
        self._lock = threading.RLock()
        self._settings = {}          # key -> descriptor dict
        self._current_module = None  # set by loader during register()

    # ── declaration ──────────────────────────────────────────────────────
    def declare(self, key, *, default=None, save=True, validate=None,
                on_change=None, owner=None):
        """Register a setting. Last declaration wins (module reload safe).

        Returns the descriptor. Declaring the same key twice replaces the
        prior one; declare core settings before modules so a module can
        deliberately override a core default if it owns the feature.
        """
        owner = owner or self._current_module or "core"
        with self._lock:
            self._settings[key] = {
                "key": key, "default": default, "save": bool(save),
                "validate": validate, "on_change": on_change, "owner": owner}
            return self._settings[key]

    def declares(self, key):
        return key in self._settings

    def owner(self, key):
        d = self._settings.get(key)
        return d["owner"] if d else None

    # ── defaults / persistence ───────────────────────────────────────────
    def seed_defaults(self, state):
        """Fill state with declared defaults for any key it's missing.

        Called at startup after all settings are declared. Never overwrites a
        value already present (loaded config wins over a default)."""
        with self._lock:
            for key, d in self._settings.items():
                if key not in state:
                    state[key] = d["default"]

    def save_keys(self):
        """The declared keys that should be persisted."""
        with self._lock:
            return [k for k, d in self._settings.items() if d["save"]]

    # ── applying an update ───────────────────────────────────────────────
    def apply(self, key, value, state):
        """Validate + store one incoming setting, fire its change handler.

        Returns (handled, error). handled=False means the key isn't declared
        here (caller falls back to legacy handling). On a validation failure
        returns (True, "reason") without storing. The change handler runs only
        when the value actually changed, and after state is updated.
        """
        d = self._settings.get(key)
        if d is None:
            return False, None
        old = state.get(key)
        if d["validate"]:
            try:
                value = d["validate"](value)
            except Exception as e:
                return True, str(e) or "invalid value"
            if value is None:
                return True, "invalid value"
        state[key] = value
        if d["on_change"] and value != old:
            try:
                d["on_change"](value, old)
            except Exception as e:
                # A handler failure shouldn't lose the value; log via return.
                return True, f"applied, handler error: {e}"
        return True, None

    def status(self):
        with self._lock:
            return [{"key": k, "owner": d["owner"], "save": d["save"],
                     "has_validator": bool(d["validate"]),
                     "has_handler": bool(d["on_change"])}
                    for k, d in self._settings.items()]


# process-wide singleton
config = ConfigRegistry()
