"""
Model capability broker.
======================================================================
A capability broker, not a model catalogue. The unit is a *capability*
("detect", "segment", "pose"), not a model. Several modules may each
provide a model that satisfies the same capability — YOLO today, Mayuki
tomorrow — and the user picks which provider serves each capability. A
consumer never asks for a model by name; it asks the broker for a
capability and gets back the selected provider's ready-to-use handle, or a
typed error if nothing satisfies it.

    consumer:   handle = broker.request("detect")
                boxes  = handle(image)          # normalized boxes, always
    module:     broker.provide("detect", "yolo11", loader=..., ...)

Because two providers for one capability may speak different native
formats (YOLO .txt boxes vs Mayuki COCO), each provider registers a
`transform` that maps its raw model output to the capability's CANONICAL
shape. The consumer gets the same shape no matter which provider ran; the
provider is responsible for handing data back in the default form.

Contracts
---------
A capability has a contract: a human description of the canonical input
and output shape every provider must honour. The core declares an initial
set (see modules/model_contracts.py). A module may declare a NEW
capability; the first declarer owns its contract. Re-declaring an existing
capability id is rejected unless the contract matches.

This module holds no torch/ML imports and does not know about YOLO. The
YOLO providers live in their own module (modules/yolo/) and register here.
Everything ML-specific is on the provider side of the seam.
"""

import threading

# Which run is being served: "fg" (the manual button / pipeline) or "bg" (the
# on-every-image sweep). Set by request(role=...) while binding, so a provider
# loader that asks variant(cap) gets the pick for the run it's serving.
_ROLE = threading.local()


class BrokerError(Exception):
    """Base for broker errors."""


class NoProviderError(BrokerError):
    """Raised by request() when a capability has no usable provider.

    Consumers are expected to catch this and degrade (skip the step, show a
    'configure a model' hint, etc.) rather than crash. Carries the
    capability id and a machine-readable `reason` in {"unknown_capability",
    "no_providers", "selected_unavailable", "none_available"}.
    """
    def __init__(self, capability, reason, message):
        super().__init__(message)
        self.capability = capability
        self.reason = reason


class Capability:
    """A named slot with a canonical I/O contract that providers satisfy."""
    def __init__(self, cap_id, *, summary, input, output, owner, label=None,
                 hidden=False, background=False):
        self.id = cap_id
        self.label = label or cap_id  # picker heading
        self.hidden = bool(hidden)    # internal capability: kept out of the picker
        # background=True: output is region-shaped and useful unprompted, so
        # the picker offers "run on every image" + a class whitelist. Caps
        # whose output is gibberish without a topic (classify, depth) stay off.
        self.background = bool(background)
        self.summary = summary        # one line: what it does
        self.input = input            # human description of canonical input
        self.output = output          # human description of canonical output
        self.owner = owner            # module id that declared it ('core' for built-ins)

    def contract(self):
        return {"input": self.input, "output": self.output}

    def as_dict(self):
        return {"id": self.id, "label": self.label, "hidden": self.hidden,
                "background": self.background, "summary": self.summary,
                "input": self.input, "output": self.output, "owner": self.owner}


class Provider:
    """One module's model registered against one capability."""
    def __init__(self, cap_id, provider_id, *, label, loader, transform=None,
                 available=None, reason="", cost_mb=0, gpu=False, module_id=None,
                 handles=None, family=None, sizes=None, types=None, settings=None,
                 classes=None, prompted=False, note="", speed="", supports_conf=None):
        self.capability = cap_id
        # note: one-liner on when to use this model (shown under the picker);
        # speed: rough cost class "fast" | "balanced" | "accurate".
        self.note = note or ""
        self.speed = speed or ""
        # supports_conf: the handle honours conf=<0..1>; the picker then offers
        # a min-confidence input. Defaults on for region-producing caps.
        self.supports_conf = (cap_id.split(".")[0] in ("detect", "segment", "pose")
                              if supports_conf is None else bool(supports_conf))
        # prompted=True: the handle needs a text prompt (vision LLM, open-vocab
        # detector); such a provider is a foreground-only choice — a background
        # sweep has no prompt to give it.
        self.prompted = bool(prompted)
        # classes() -> ordered list of class names the model emits (may load
        # weights). Feeds the background-processing whitelist; None = unknown.
        self._classes = classes
        self.id = provider_id                 # unique within the capability
        self.label = label                    # human label for the picker
        # Variant axes the picker shows for this provider. sizes: list of
        # size ids ("n","s",…); types: list of {value,label} (e.g. pose 17 vs
        # whole-body); either empty => that select is greyed out. settings:
        # extra widgets [{key,label,kind,options?,help?}] bound to config keys
        # the module declared (same kinds as host.add_settings_field).
        self.family = family or label
        self.sizes = list(sizes or [])
        self.types = [t if isinstance(t, dict) else {"value": t, "label": t}
                      for t in (types or [])]
        self.settings = list(settings or [])
        self._loader = loader                 # () -> raw model handle (cached upstream)
        self._transform = transform           # raw_output -> canonical output
        self._available = available           # () -> bool, or None => always available
        self._reason = reason                 # why unavailable, for the UI
        self.cost_mb = cost_mb
        self.gpu = gpu
        self.module_id = module_id
        # Optional predicate handles(model_path)->bool. For path-parameterized
        # capabilities like 'box', where several providers coexist and the right
        # one is chosen by which model file it can run (YOLO .pt vs Mayaku).
        self._handles = handles

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
        return "" if self.available() else (self._reason or "unavailable")

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

    # ── the handle a consumer actually calls ─────────────────────────────
    def bind(self):
        """Return a callable handle that runs the model and normalizes output.

        The loader is expected to be cheap-on-repeat (the YOLO providers back
        it with the runtime model_registry LRU), so bind() can be called per
        request. The returned callable applies the provider's transform so the
        consumer always receives the capability's canonical shape.
        """
        model = self._loader()
        transform = self._transform

        def run(*args, **kwargs):
            raw = model(*args, **kwargs) if callable(model) else model
            return transform(raw, *args, **kwargs) if transform else raw
        run.model = model            # escape hatch: raw handle if a caller needs it
        run.provider = self
        return run


class ModelBroker:
    """Registry of capabilities + providers, with per-capability selection."""

    def __init__(self):
        self._lock = threading.RLock()
        self._caps = {}                       # cap_id -> Capability
        self._providers = {}                  # cap_id -> {provider_id -> Provider}
        self._selection = {}                  # cap_id -> provider_id (user choice)
        self._variant = {}                    # cap_id -> {"size","type",...} (user choice)
        # Optional separate pick for the background sweep: cap_id ->
        # {"provider","size","type"}. Absent = same model as foreground.
        self._bg = {}
        self._on_select = []                  # fns(cap_id) run after a selection changes
        self._current_module = None           # set by loader during register()

    # ── capability declaration ───────────────────────────────────────────
    def declare(self, cap_id, *, summary, input, output, owner=None, label=None,
                hidden=False, background=False):
        """Declare a capability contract. First declarer wins.

        Re-declaring an existing id is allowed only if the contract matches
        (same input/output text); a conflicting redeclare raises. This lets
        the core and a module both name the same capability without fighting,
        while catching two modules that disagree on the shape.
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

    # ── provider registration ────────────────────────────────────────────
    def provide(self, cap_id, provider_id, *, label, loader, transform=None,
                available=None, reason="", cost_mb=0, gpu=False, handles=None,
                family=None, sizes=None, types=None, settings=None, classes=None,
                prompted=False, note="", speed="", supports_conf=None):
        """Register a provider for a capability. Dedup by (cap_id, provider_id).

        The predefined capabilities (model_contracts) exist so providers of the
        same job stay interchangeable; a module may still provide for an
        arbitrary capability id, which is declared on the fly with a generic
        contract owned by that module (declare it explicitly to document I/O).
        Re-providing the same id replaces the earlier registration (last
        writer wins), which is what you want when a module is reloaded.
        """
        with self._lock:
            if cap_id not in self._caps:
                self._caps[cap_id] = Capability(
                    cap_id, summary=f"Module-defined capability '{cap_id}'.",
                    input="see the providing module", output="see the providing module",
                    owner=self._current_module or "module",
                    label=cap_id.replace(".", " · ").replace("_", " ").title())
                self._providers.setdefault(cap_id, {})
            p = Provider(cap_id, provider_id, label=label, loader=loader,
                         transform=transform, available=available, reason=reason,
                         cost_mb=cost_mb, gpu=gpu,
                         module_id=self._current_module, handles=handles,
                         family=family, sizes=sizes, types=types, settings=settings,
                         classes=classes, prompted=prompted, note=note, speed=speed,
                         supports_conf=supports_conf)
            self._providers[cap_id][provider_id] = p
            return p

    # ── selection ────────────────────────────────────────────────────────
    def select(self, cap_id, provider_id, size=None, type=None,
               background=None, classes=None, bg=None, conf=None):
        """Set the user's chosen provider (+ size/type variant) for a
        capability. Returns (ok, err).

        Selecting an unknown capability or provider fails; a size/type the
        provider doesn't declare is dropped (falls back to its first). Selecting
        a currently-unavailable provider is ALLOWED (the weights may appear
        later); request() will surface the unavailability at call time.
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
            # bg: {"provider","size","type"} for a different background model;
            # {} / None / a provider not registered => same as foreground.
            bp = (bg or {}).get("provider") if isinstance(bg, dict) else None
            pb = self._providers[cap_id].get(bp) if bp else None
            if pb is None or pb.prompted:      # a prompted model can't run unprompted
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
        """Register fn(cap_id) to run after any selection change (a module can
        keep a side-effect, e.g. a checkpoint path, in sync with the pick)."""
        self._on_select.append(fn)

    def selected_id(self, cap_id, role=None):
        """The chosen provider id for a capability (role "bg" = the background
        sweep's own pick when set), or the default.

        Default = first-registered provider that is currently available; if
        none is available, the first registered at all (so the UI shows a
        sensible pre-selection even when weights are missing).
        """
        with self._lock:
            role = role or getattr(_ROLE, "value", None)
            if role == "bg" and cap_id in self._bg:
                return self._bg[cap_id]["provider"]
            sel = self._selection.get(cap_id)
            provs = self._providers.get(cap_id, {})
            if sel and sel in provs:
                return sel
            # Default: an available UNPROMPTED provider first (a vision LLM is
            # never a sensible default — it needs a prompt and costs a call).
            for pid, p in provs.items():
                if p.available() and not p.prompted:
                    return pid
            for pid, p in provs.items():
                if p.available():
                    return pid
            return next(iter(provs), None)

    def variant(self, cap_id, role=None):
        """{"size","type","background","classes"} in effect for a capability
        (role "bg" = the background sweep's own model when one is set): the
        user's choice when the selected provider declares it, else the
        provider's first option, else None. Providers read this in their
        loaders; inside request() the role is implied."""
        with self._lock:
            role = role or getattr(_ROLE, "value", None)
            pid = self.selected_id(cap_id, role)
            p = self._providers.get(cap_id, {}).get(pid)
            v = self._variant.get(cap_id, {})
            if role == "bg" and cap_id in self._bg:
                v = {**v, **self._bg[cap_id]}
            if p is None:
                return {"size": None, "type": None, "background": False, "classes": []}
            size = v.get("size") if v.get("size") in p.sizes else (p.sizes[0] if p.sizes else None)
            tvals = [t["value"] for t in p.types]
            typ = v.get("type") if v.get("type") in tvals else (tvals[0] if tvals else None)
            base = self._variant.get(cap_id, {})
            return {"size": size, "type": typ,
                    "background": bool(base.get("background")),
                    "classes": list(base.get("classes") or []),
                    "conf": float(base.get("conf", 0.25))}

    def background_capabilities(self):
        """[cap_id] whose background run is switched on and whose background
        provider (own pick, else the foreground one) is unprompted."""
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
        """Class names the *background* provider for cap_id emits ([] if
        unknown) — the whitelist filters the unprompted run, so it lists what
        that model was trained on."""
        with self._lock:
            p = self._providers.get(cap_id, {}).get(self.selected_id(cap_id, "bg"))
        return p.classes() if p else []

    def init_selection(self, persisted):
        """Seed selections from persisted config.

        Accepts {cap_id: provider_id} (legacy) or {cap_id: {provider, size,
        type}}. Unknown capabilities / providers are dropped, so removing a
        module doesn't leave a dangling selection. Returns the cleaned map to
        write back.
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
        """{cap_id: {provider, size, type}} to persist — explicit choices only."""
        with self._lock:
            return {c: {"provider": pid, **self._variant.get(c, {}),
                        **({"bg": self._bg[c]} if c in self._bg else {})}
                    for c, pid in self._selection.items()}

    # ── the consumer entry point ─────────────────────────────────────────
    def request(self, cap_id, role="fg", provider=None):
        """Return a ready callable handle for the selected provider. role "bg"
        serves the background sweep, which may have its own model pick.
        provider=<id> bypasses the selection (a consumer that needs a specific
        kind, e.g. SAM 2 needing a prompted detector for seed boxes).

        Raises NoProviderError (a typed error the consumer handles) when:
          - the capability was never declared        (unknown_capability)
          - no provider is registered for it          (no_providers)
          - a provider is selected but unavailable     (selected_unavailable)
          - nothing registered is available            (none_available)
        The returned handle normalizes output to the capability's canonical
        shape via the provider's transform.
        """
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
            prev = getattr(_ROLE, "value", None)
            _ROLE.value = role          # loaders resolve variant() for this run
            try:
                if sel and sel in provs:
                    p = provs[sel]
                    if not p.available():
                        raise NoProviderError(cap_id, "selected_unavailable",
                            f"selected model '{p.label}' for '{cap_id}' is "
                            f"unavailable: {p.reason()}")
                    return p.bind()
                # no explicit selection: the same default selected_id() shows
                # (available and unprompted first, then any available)
                pid = self.selected_id(cap_id, role)
                p = provs.get(pid)
                if p is not None and p.available():
                    return p.bind()
                raise NoProviderError(cap_id, "none_available",
                    f"no available model for '{cap_id}'")
            finally:
                _ROLE.value = prev

    def detector_for(self, cap_id, model_path):
        """Pick the provider that can run `model_path` for a path-parameterized
        capability (e.g. 'box'). Returns a bound detect handle or None.

        Providers declare handles(model_path); the first available one whose
        predicate matches wins, with the user-selected provider preferred when
        it also matches. Returns None when nothing handles the path, so the
        caller can fall back to its legacy path (keeps the migration safe).
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
                    # Return the loader's detect fn directly (not bind()'s
                    # wrapper) so attributes like .batch survive; box providers
                    # already return the canonical shape, so no transform wrap.
                    try:
                        return p._loader()
                    except Exception:
                        return None
        return None

    def try_request(self, cap_id):
        """request() that returns None instead of raising. For callers that
        genuinely want to probe-and-skip without a try/except."""
        try:
            return self.request(cap_id)
        except NoProviderError:
            return None

    # ── introspection for the settings UI ────────────────────────────────
    def providers_for(self, cap_id):
        with self._lock:
            return [p.as_dict() for p in self._providers.get(cap_id, {}).values()]

    def status(self):
        """Full snapshot: every capability, its providers, and the selection."""
        with self._lock:
            out = []
            for cap_id, cap in self._caps.items():
                out.append({
                    **cap.as_dict(),
                    "selected": self.selected_id(cap_id, "fg"),
                    "variant": self.variant(cap_id, "fg"),
                    # background sweep's own pick, or None (= same as foreground)
                    "bg": ({"provider": self._bg[cap_id]["provider"],
                            **{k: v for k, v in self.variant(cap_id, "bg").items()
                               if k in ("size", "type")}}
                           if cap_id in self._bg else None),
                    "providers": [p.as_dict()
                                  for p in self._providers.get(cap_id, {}).values()],
                })
            return out


# process-wide singleton; modules/__init__.py exposes it, manager wires it in
broker = ModelBroker()
