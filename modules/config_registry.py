"""! @file
@brief The settings registry: what settings exist, their defaults, validation,
persistence and change handlers. Core and modules declare here; the live
values stay in the state dict.
"""

import threading


class ConfigRegistry:
    def __init__(self):
        self._lock = threading.RLock()
        self._settings = {}
        self._current_module = None  # set by the loader while a module registers

    def declare(self, key, *, default=None, save=True, validate=None,
                on_change=None, owner=None):
        """! @brief Declare a setting; declaring a key again replaces it.
        @param default    value when absent.
        @param save       persist to app_config.json.
        @param validate   fn(value) -> cleaned value; raise or return None to reject.
        @param on_change  fn(new, old), run after a saved change.
        @return the descriptor.
        """
        owner = owner or self._current_module or "core"
        with self._lock:
            self._settings[key] = {
                "key": key, "default": default, "save": bool(save),
                "validate": validate, "on_change": on_change, "owner": owner}
            return self._settings[key]

    def declares(self, key):
        return key in self._settings

    def default_of(self, key):
        d = self._settings.get(key)
        return d["default"] if d else None

    def owner(self, key):
        d = self._settings.get(key)
        return d["owner"] if d else None

    def seed_defaults(self, state, saved=None):
        """! @brief Fill missing keys of `state` with the saved value, else the default.
        @param saved  the raw config file, so a module declared after loading keeps its value.
        """
        saved = saved or {}
        with self._lock:
            for key, d in self._settings.items():
                if key not in state:
                    state[key] = saved[key] if key in saved else d["default"]

    def save_keys(self):
        """! @brief Keys that are persisted."""
        with self._lock:
            return [k for k, d in self._settings.items() if d["save"]]

    def apply(self, key, value, state):
        """! @brief Validate and store one setting, then run its change handler if it changed.
        @return (handled, error): handled is False for undeclared keys; error is the
                rejection reason, or "applied, handler error: ..." when only the
                handler failed.
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
                # The value is stored even when its handler fails.
                return True, f"applied, handler error: {e}"
        return True, None

    def status(self):
        with self._lock:
            return [{"key": k, "owner": d["owner"], "save": d["save"],
                     "has_validator": bool(d["validate"]),
                     "has_handler": bool(d["on_change"])}
                    for k, d in self._settings.items()]


config = ConfigRegistry()
