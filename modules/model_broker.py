"""! @file
@brief The model capability broker.

Consumers ask for a capability ("detect", "segment", "pose"), never a model:

    handle = broker.request("detect")
    boxes = handle(image)                  # the capability's canonical shape
    broker.provide("detect", "yolo11", loader=..., transform=...)

Modules provide models for capabilities; the user picks which provider serves
each one. A provider's transform turns its native output into the contract's
canonical shape (contracts: model_contracts.py). No ML imports here.

A provider runs one call at a time: background sweeps and foreground requests
share its cached model across worker threads, and many native runtimes (TFLite
interpreters, MediaPipe graphs, Ultralytics predictors, cv2.dnn nets) crash the
process - a segfault, no Python error - when two threads call the same
instance. Different providers still run in parallel; providers on a remote
backend (`resource`) aren't serialized.
"""

import threading

# "fg" (button / pipeline) or "bg" (background sweep), set while request() binds,
# so a loader reading variant() gets the pick of the run it serves.
_ROLE = threading.local()


class BrokerError(Exception):
    """! @brief Base of broker errors."""


class NoProviderError(BrokerError):
    """! @brief No usable provider for a capability; consumers catch it and degrade.
    `reason`: unknown_capability | no_providers | selected_unavailable | none_available.
    """
    def __init__(self, capability, reason, message):
        super().__init__(message)
        self.capability = capability
        self.reason = reason


class Guarded:
    """! @brief A model handle whose calls (and .batch) hold `lock`; every other
    attribute is the model's own."""

    def __init__(self, model, lock):
        object.__setattr__(self, "_model", model)
        object.__setattr__(self, "_lock", lock)
        if hasattr(model, "batch"):
            def batch(*args, **kwargs):
                with lock:
                    return model.batch(*args, **kwargs)
            object.__setattr__(self, "batch", batch)

    def __call__(self, *args, **kwargs):
        with self._lock:
            return self._model(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._model, name)


class Capability:
    """! @brief A named slot with a canonical input / output contract."""
    def __init__(self, cap_id, *, summary, input, output, owner, label=None,
                 hidden=False, background=False):
        self.id = cap_id
        self.label = label or cap_id  # picker heading
        self.hidden = bool(hidden)  # kept out of the picker
        # output is region-shaped and useful unprompted: offer "run on every image"
        self.background = bool(background)
        self.summary = summary
        self.input = input
        self.output = output
        self.owner = owner  # declaring module ('core' for built-ins)

    def contract(self):
        return {"input": self.input, "output": self.output}

    def as_dict(self):
        return {"id": self.id, "label": self.label, "hidden": self.hidden,
                "background": self.background, "summary": self.summary,
                "input": self.input, "output": self.output, "owner": self.owner}


class Provider:
    """! @brief One module's model registered for one capability."""
    def __init__(self, cap_id, provider_id, *, label, loader, transform=None,
                 available=None, reason="", cost_mb=0, gpu=False, module_id=None,
                 handles=None, family=None, sizes=None, types=None, settings=None,
                 classes=None, prompted=False, note="", speed="", supports_conf=None,
                 resource=None, concurrency=1):
        self.capability = cap_id
        # providers on the same external backend share its parallel budget
        # (concurrency: int or callable); None = local, no cap
        self.resource = resource
        self.concurrency = concurrency
        # note: shown under the picker; speed: fast | balanced | accurate
        self.note = note or ""
        self.speed = speed or ""
        # the handle accepts conf=0..1 (default for region capabilities)
        self.supports_conf = (cap_id.split(".")[0] in ("detect", "segment", "pose")
                              if supports_conf is None else bool(supports_conf))
        # needs a text prompt: foreground only
        self.prompted = bool(prompted)
        # fn() -> class names the model emits, for the background whitelist
        self._classes = classes
        self.id = provider_id  # unique within the capability
        self.label = label
        # variant axes for the picker: sizes, types [{value, label}], extra settings widgets
        self.family = family or label
        self.sizes = list(sizes or [])
        self.types = [t if isinstance(t, dict) else {"value": t, "label": t}
                      for t in (types or [])]
        self.settings = list(settings or [])
        self._loader = loader  # fn() -> raw model handle (cached upstream)
        self._transform = transform  # raw output -> canonical output
        self._available = available  # fn() -> bool; None = always available
        self._reason = reason  # why unavailable, for the UI
        self.cost_mb = cost_mb
        self.gpu = gpu
        self.module_id = module_id
        # fn(model_path) -> bool, for path-parameterised capabilities ('box')
        self._handles = handles
        # one call at a time into this provider's models (see the module note)
        self._call_lock = threading.RLock()

    def classes(self):
        if self._classes is None:
            return []
        try:
            return [str(c) for c in (self._classes() or [])]
        except Exception:
            return []

    def handles(self, model_path):
        if self._handles is None:
            return False
        try:
            return bool(self._handles(model_path))
        except Exception:
            return False

    def available(self):
        if self._available is None:
            return True
        try:
            return bool(self._available())
        except Exception:
            return False

    def reason(self):
        if self.available():
            return ""
        r = self._reason
        if callable(r):  # say what actually went wrong
            try:
                r = r()
            except Exception:
                r = ""
        return r or "unavailable"

    @property
    def key(self):
        return f"{self.capability}:{self.id}"

    def limit(self):
        """! @brief Parallel budget of this provider's resource (at least 1)."""
        try:
            c = self.concurrency() if callable(self.concurrency) else self.concurrency
            return max(1, int(c or 1))
        except Exception:
            return 1

    def as_dict(self):
        return {"id": self.id, "label": self.label, "family": self.family,
                "sizes": self.sizes, "types": self.types,
                "has_classes": self._classes is not None,
                "prompted": self.prompted, "note": self.note, "speed": self.speed,
                "supports_conf": self.supports_conf,
                "settings": [dict(f) for f in self.settings],
                "available": self.available(), "reason": self.reason(),
                "cost_mb": self.cost_mb, "gpu": self.gpu,
                "module_id": self.module_id}

    def bind(self):
        """! @brief A callable that runs the model and returns the canonical shape.
        Cheap to call per request: loaders cache in model_registry.
        """
        model = self._loader()
        if model is None:
            # A missing model must not look like an empty result.
            raise RuntimeError(f"{self.capability}:{self.id} has no model to run "
                               f"(its loader returned None)")
        transform = self._transform
        lock = None if self.resource is not None else self._call_lock

        def run(*args, **kwargs):
            if lock is None:
                raw = model(*args, **kwargs) if callable(model) else model
            else:
                with lock:
                    raw = model(*args, **kwargs) if callable(model) else model
            return transform(raw, *args, **kwargs) if transform else raw
        run.model = model  # raw handle, for callers that need it
        run.provider = self
        # keep a provider's extra entry points (batch, model_path, registry_key)
        for k in ("model_path", "registry_key"):
            if hasattr(model, k):
                setattr(run, k, getattr(model, k))
        if hasattr(model, "batch"):
            run.batch = model.batch if lock is None else Guarded(model, lock).batch
        return run

    def guard(self, model):
        """! @brief `model` with its calls serialized (see the module note), or as is for a
        remote backend or no model."""
        if model is None or self.resource is not None:
            return model
        return Guarded(model, self._call_lock)


class ModelBroker:
    """! @brief Capabilities, their providers and the user's pick per capability."""

    def __init__(self):
        self._lock = threading.RLock()
        self._caps = {}  # cap_id -> Capability
        self._providers = {}  # cap_id -> {provider_id -> Provider}
        self._selection = {}  # cap_id -> provider_id
        self._variant = {}  # cap_id -> {"size", "type", ...}
        # cap_id -> {"provider", "size", "type"} for the background sweep; absent = same as foreground
        self._bg = {}
        self._on_select = []  # fn(cap_id) after a selection change
        self._current_module = None  # set by the loader while a module registers

    def declare(self, cap_id, *, summary, input, output, owner=None, label=None,
                hidden=False, background=False):
        """! @brief Declare a capability contract; the first declarer owns it.
        @throws ValueError when a re-declaration's contract differs.
        """
        owner = owner or self._current_module or "core"
        with self._lock:
            existing = self._caps.get(cap_id)
            if existing is not None:
                if (existing.input, existing.output) != (input, output):
                    raise BrokerError(
                        f"capability '{cap_id}' already declared by "
                        f"'{existing.owner}' with a different contract")
                return existing
            cap = Capability(cap_id, summary=summary, input=input,
                             output=output, owner=owner, label=label,
                             hidden=hidden, background=background)
            self._caps[cap_id] = cap
            self._providers.setdefault(cap_id, {})
            return cap

    def has_capability(self, cap_id):
        return cap_id in self._caps

    def provide(self, cap_id, provider_id, *, label, loader, transform=None,
                available=None, reason="", cost_mb=0, gpu=False, handles=None,
                family=None, sizes=None, types=None, settings=None, classes=None,
                prompted=False, note="", speed="", supports_conf=None,
                resource=None, concurrency=1, module_id=None):
        """! @brief Register (or replace) a provider for a capability. An undeclared
        capability is declared with a generic contract owned by the module.
        """
        module_id = module_id or self._current_module
        with self._lock:
            if cap_id not in self._caps:
                self._caps[cap_id] = Capability(
                    cap_id, summary=f"Module-defined capability '{cap_id}'.",
                    input="see the providing module", output="see the providing module",
                    owner=module_id or "module",
                    label=cap_id.replace(".", " | ").replace("_", " ").title())
                self._providers.setdefault(cap_id, {})
            p = Provider(cap_id, provider_id, label=label, loader=loader,
                         transform=transform, available=available, reason=reason,
                         cost_mb=cost_mb, gpu=gpu,
                         module_id=module_id, handles=handles,
                         family=family, sizes=sizes, types=types, settings=settings,
                         classes=classes, prompted=prompted, note=note, speed=speed,
                         supports_conf=supports_conf,
                resource=resource, concurrency=concurrency)
            self._providers[cap_id][provider_id] = p
            tm = getattr(self, "thread_manager", None)
            if tm is not None:  # tell the thread manager what it runs on and how many at once
                tm.register_model(p.key, gpu=gpu, resource=resource, concurrency=concurrency,
                                  cost_mb=cost_mb)
            return p

    def select(self, cap_id, provider_id, size=None, type=None,
               background=None, classes=None, bg=None, conf=None):
        """! @brief Pick the provider (and size / type) of a capability. An unavailable
        provider may be picked; an undeclared size / type falls back to the first.
        @param bg  {"provider", "size", "type"} for a separate background model.
        @return (ok, error).
        """
        with self._lock:
            if cap_id not in self._caps:
                return False, "unknown capability"
            p = self._providers.get(cap_id, {}).get(provider_id)
            if p is None:
                return False, "unknown provider"
            self._selection[cap_id] = provider_id
            v = {}
            if size in p.sizes:
                v["size"] = size
            if type in [t["value"] for t in p.types]:
                v["type"] = type
            if background is not None and self._caps[cap_id].background:
                v["background"] = bool(background)
            if classes is not None:
                v["classes"] = [str(c) for c in classes]
            if conf is not None:
                try:
                    v["conf"] = max(0.0, min(1.0, float(conf)))
                except (TypeError, ValueError):
                    pass
            self._variant[cap_id] = v
            bp = (bg or {}).get("provider") if isinstance(bg, dict) else None
            pb = self._providers[cap_id].get(bp) if bp else None
            if pb is None or pb.prompted:  # a prompted model can't run unprompted
                self._bg.pop(cap_id, None)
            else:
                b = {"provider": bp}
                if bg.get("size") in pb.sizes:
                    b["size"] = bg["size"]
                if bg.get("type") in [t["value"] for t in pb.types]:
                    b["type"] = bg["type"]
                self._bg[cap_id] = b
        for fn in list(self._on_select):
            try:
                fn(cap_id)
            except Exception:
                pass
        return True, None

    def on_select(self, fn):
        """! @brief Run fn(cap_id) after every selection change."""
        self._on_select.append(fn)

    def selected_id(self, cap_id, role=None):
        """! @brief The picked provider id (role "bg": the background pick when set).
        Default: the first available unprompted provider, else the first registered.
        """
        with self._lock:
            # While binding, answer with the provider being bound so request(cap, provider=X)
            # gets X's own size / type.
            bound = getattr(_ROLE, "binding", None)
            if bound and bound[0] == cap_id:
                return bound[1]
            role = role or getattr(_ROLE, "value", None)
            if role == "bg" and cap_id in self._bg:
                return self._bg[cap_id]["provider"]
            sel = self._selection.get(cap_id)
            provs = self._providers.get(cap_id, {})
            if sel and sel in provs:
                return sel
            # default: an available unprompted provider (a vision LLM is never a sensible default)
            for pid, p in provs.items():
                if p.available() and not p.prompted:
                    return pid
            for pid, p in provs.items():
                if p.available():
                    return pid
            return next(iter(provs), None)

    def variant(self, cap_id, role=None, provider=None):
        """! @brief {"size", "type", "background", "classes"} in effect: the user's choice when
        the provider declares it, else its first option.
        @param role      "bg" for the background model.
        @param provider  resolve against this provider instead of the picked one.
        """
        with self._lock:
            role = role or getattr(_ROLE, "value", None)
            pid = provider or self.selected_id(cap_id, role)
            p = self._providers.get(cap_id, {}).get(pid)
            v = self._variant.get(cap_id, {})
            if role == "bg" and cap_id in self._bg:
                v = {**v, **self._bg[cap_id]}
            if p is None:
                base = self._variant.get(cap_id, {})
                return {"size": None, "type": None, "background": False, "classes": [],
                        "conf": float(base.get("conf", 0.25))}
            size = v.get("size") if v.get("size") in p.sizes else (p.sizes[0] if p.sizes else None)
            tvals = [t["value"] for t in p.types]
            typ = v.get("type") if v.get("type") in tvals else (tvals[0] if tvals else None)
            base = self._variant.get(cap_id, {})
            return {"size": size, "type": typ,
                    "background": bool(base.get("background")),
                    "classes": list(base.get("classes") or []),
                    "conf": float(base.get("conf", 0.25))}

    def provider_for(self, cap_id, role="fg"):
        """! @brief The picked Provider (role "bg": the background pick), or None."""
        with self._lock:
            return self._providers.get(cap_id, {}).get(self.selected_id(cap_id, role))

    def background_capabilities(self):
        """! @brief Capabilities with background run on and an unprompted background provider."""
        with self._lock:
            out = []
            for c, cap in self._caps.items():
                if not (cap.background and self._variant.get(c, {}).get("background")):
                    continue
                p = self._providers.get(c, {}).get(self.selected_id(c, "bg"))
                if p is not None and not p.prompted:
                    out.append(c)
            return out

    def provider_classes(self, cap_id):
        """! @brief Class names the background provider emits ([] when unknown)."""
        with self._lock:
            p = self._providers.get(cap_id, {}).get(self.selected_id(cap_id, "bg"))
        return p.classes() if p else []

    def init_selection(self, persisted):
        """! @brief Load saved picks ({cap: provider} or {cap: {provider, size, type}});
        unknown capabilities and providers are dropped.
        @return the cleaned map to save back.
        """
        persisted = persisted or {}
        with self._lock:
            self._selection, self._variant, self._bg = {}, {}, {}
            for cap_id, v in persisted.items():
                if isinstance(v, str):
                    v = {"provider": v}
                if not isinstance(v, dict):
                    continue
                self.select(cap_id, v.get("provider"), v.get("size"), v.get("type"),
                            v.get("background"), v.get("classes"), v.get("bg"),
                            v.get("conf"))
            return self.current_selection()

    def current_selection(self):
        """! @brief The explicit picks to save: {cap_id: {provider, size, type}}."""
        with self._lock:
            return {c: {"provider": pid, **self._variant.get(c, {}),
                        **({"bg": self._bg[c]} if c in self._bg else {})}
                    for c, pid in self._selection.items()}

    def request(self, cap_id, role="fg", provider=None):
        """! @brief A ready handle for a capability's picked provider.
        @param role      "bg" serves the background sweep.
        @param provider  use this provider instead of the pick.
        @throws NoProviderError (unknown_capability, no_providers,
                selected_unavailable, none_available).
        """
        # Resolve under the lock, load outside it: a slow model load must not stall
        # status() (the Models tab). model_registry has its own per-key load lock.
        with self._lock:
            if cap_id not in self._caps:
                raise NoProviderError(cap_id, "unknown_capability",
                    f"no such capability '{cap_id}'")
            provs = self._providers.get(cap_id, {})
            if not provs:
                raise NoProviderError(cap_id, "no_providers",
                    f"no model is registered for '{cap_id}'")
            sel = provider or (self._bg[cap_id]["provider"] if role == "bg" and cap_id in self._bg
                               else self._selection.get(cap_id))
            if sel and sel in provs:
                p = provs[sel]
                if not p.available():
                    raise NoProviderError(cap_id, "selected_unavailable",
                        f"selected model '{p.label}' for '{cap_id}' is "
                        f"unavailable: {p.reason()}")
            else:
                p = provs.get(self.selected_id(cap_id, role))
                if p is None or not p.available():
                    raise NoProviderError(cap_id, "none_available",
                        f"no available model for '{cap_id}'")
        prev = getattr(_ROLE, "value", None)
        prev_bind = getattr(_ROLE, "binding", None)
        _ROLE.value = role  # loaders resolve variant() for this run
        _ROLE.binding = (cap_id, p.id)
        try:
            return p.bind()
        finally:
            _ROLE.value = prev
            _ROLE.binding = prev_bind

    def detector_for(self, cap_id, model_path):
        """! @brief A detect handle for `model_path` from the provider that handles it (the
        picked provider preferred).
        @return the handle, or None when nothing handles the path.
        """
        with self._lock:
            provs = self._providers.get(cap_id, {})
            if not provs:
                return None
            sel = self._selection.get(cap_id)
            order = ([provs[sel]] if sel and sel in provs else []) + \
                    [p for pid, p in provs.items() if pid != sel]
            for p in order:
                if p.available() and p.handles(model_path):
                    # the loader's own function, so .batch survives
                    try:
                        return p._loader()
                    except Exception:
                        return None
        return None

    def try_request(self, cap_id):
        """! @brief request(), but None instead of raising."""
        try:
            return self.request(cap_id)
        except NoProviderError:
            return None

    def providers_for(self, cap_id):
        with self._lock:
            return [p.as_dict() for p in self._providers.get(cap_id, {}).values()]

    def status(self):
        """! @brief Every capability with its providers and picks."""
        with self._lock:
            out = []
            for cap_id, cap in self._caps.items():
                out.append({
                    **cap.as_dict(),
                    "selected": self.selected_id(cap_id, "fg"),
                    "variant": self.variant(cap_id, "fg"),
                    # background pick, or None (= foreground)
                    "bg": ({"provider": self._bg[cap_id]["provider"],
                            **{k: v for k, v in self.variant(cap_id, "bg").items()
                               if k in ("size", "type")}}
                           if cap_id in self._bg else None),
                    "providers": [p.as_dict()
                                  for p in self._providers.get(cap_id, {}).values()],
                })
            return out


broker = ModelBroker()
