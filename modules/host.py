"""! @file
@brief The Host: everything a module's register(host) gets from the core.

Modules build against this object, never against manager.py. The host records
what a module contributes (routes, assets, settings, tables, workers, hooks)
and manager.py wires those in after loading. Nothing here imports manager.py.
"""
import os
import time

class Host:
    """! @brief The API handed to each module's register(host).

    Raw handles: app, db, config, logger, thread_manager, media_dir, safe_path,
    save_config, broker, core (core helpers) and media (the media-type registry).
    Prefer the helpers below over the raw handles.
    """

    def __init__(self, *, app, db, config, logger, thread_manager,
                 media_dir, safe_path, save_config, broker=None,
                 config_registry=None, core=None, media=None):
        self.app = app
        self.db = db
        self.config = config
        self.logger = logger
        self.thread_manager = thread_manager
        self.media_dir = media_dir
        self.safe_path = safe_path
        self.save_config = save_config
        # model capability broker (see provide_model / request_model)
        self.broker = broker

        self.config_registry = config_registry
        self.core = core
        self.media = media

        # -- contributions, read back by manager.py after loading --
        # {"module_id", "filename", "kind": "js" | "css"}
        self.assets = []
        # {"id", "label", "icon", "admin_only", "assets"}
        self.settings_tabs = []
        # zero-argument callables run after the server is up
        self.startup_hooks = []
        self.event_hooks = {}  # event -> [fn(**kw)]
        # name -> {"fn", "label", "editor"}
        self.pipeline_stages = {}
        # {"ddl", "check", "module_id"}; created after all modules register
        self.db_tables = []
        self.background_sweeps = {}  # cap_id -> {pending, run, module_id}
        self._sweep_rr, self._sweep_idle, self._sweep_skip, self._sweep_done = 0, {}, set(), {}
        self._sweep_inflight = 0
        # fn(db, rel_paths) -> {rel_path: {field: value}}, merged into listing rows
        self.file_enrichers = []
        # settings-UI field descriptors (see add_settings_field)
        self.settings_fields = []
        # setting key -> owning tab, for keys saved without a settings field
        self.config_tabs = {}
        # per-user setting key -> descriptor (see add_user_setting)
        self.user_settings = {}
        # {tab_id: template}: controls-pane partials rendered server-side
        self.controls_panes = []
        # modal partials rendered into app.html
        self.app_modals = []
        self.centre_panes = []
        self.search_help = {}  # search prefix -> {help, module_id}
        self.action_targets = {}  # AI-action target -> fn(fp, bgr, meta, action)
        self.ai_action_groups = []  # see register_ai_actions
        self.gallery_filters = []  # SQL clauses that hide rows from the flat gallery
        # see register_access_policy
        self.access_policies = []
        # left-column pane partials
        self.left_panes = []
        # fn(text, folder, structured) -> [entry]
        self.search_providers = []
        # token prefix -> fn(token, value) -> (sql, params)
        self.search_types = {}
        # sort name -> ORDER BY expression (or a callable returning one)
        self.sort_keys = {}
        # {name: {"obj", "module_id", "priority"}}
        self.services = {}
        # the module being registered, so contributions are attributed to it
        self._current_module = None

    def add_route(self, rule, view_func, *, feature=None, level="read",
                  action=None, fields=(), **options):
        """! @brief Register a Flask route, gated by a feature permission.
        @param feature  permission key; None = any signed-in user.
        @param level    "read" to view, "write" to change.
        @param opts     passed to add_url_rule; action / fields feed the audit log.
        The endpoint name is namespaced by module, so two modules may both have `list`.
        """
        if feature:
            view_func = self.require_feature(feature, action=action,
                                             fields=fields, level=level)(view_func)
        endpoint = options.pop("endpoint", None)
        if endpoint is None:
            mod = self._current_module or "mod"
            endpoint = f"module_{mod}_{view_func.__name__}"
        self.app.add_url_rule(rule, endpoint, view_func, **options)

    def route(self, rule, **options):
        """! @brief Decorator form of add_route:

            @host.route("/api/thing", methods=["POST"], feature="tab.thing", level="write")
            def api_thing(): ...
        """
        def deco(fn):
            self.add_route(rule, fn, **options)
            return fn
        return deco

    def add_asset(self, filename, kind=None, module_id=None):
        """! @brief Load a JS or CSS file in the main page.
        @param filename  path inside the module's static/ folder (served at
                         /modules/<id>/static/<filename>), or an absolute /static/... URL.
        @param kind      "js" or "css"; taken from the extension when omitted.
        """
        module_id = module_id or self._current_module
        if kind is None:
            kind = "css" if filename.lower().endswith(".css") else "js"
        self.assets.append({"module_id": module_id,
                            "filename": filename, "kind": kind})

    def module_static_url(self, filename, module_id=None):
        module_id = module_id or self._current_module
        return f"/modules/{module_id}/static/{filename}"

    def add_settings_tab(self, tab_id, label, icon="", admin_only=False, group="modules"):
        """! @brief Add a Settings tab; the module's own JS fills its pane.
        @param group       rail group: "you", "server", "admin" or "modules".
        @param admin_only  block the tab for every non-admin role by default.
        The tab gets the permission settings.<tab_id>: read shows it, write saves it.
        """
        feature = self.core.features.register_settings_tab(tab_id, label, admin_only=admin_only)
        self.settings_tabs.append({
            "id": tab_id, "label": label, "icon": icon,
            "admin_only": bool(admin_only), "group": group or "modules",
            "feature": feature,
            "module_id": self._current_module,
        })

    def add_config_key(self, key, *, default=None, save=True,
                       validate=None, on_change=None, tab=None):
        """! @brief Declare a setting this module owns (default, persistence, validation).
        @param save       persist it.
        @param validate   fn(value) -> cleaned value; raise or return None to reject.
        @param on_change  fn(new, old) after a saved change.
        @param tab        the tab whose permission guards writes; only needed for a
                          key without a settings field in a module with several tabs.
        """
        self.config_registry.declare(
            key, default=default, save=save, validate=validate,
            on_change=on_change, owner=self._current_module)
        if tab:
            self.config_tabs[key] = tab

    def set_config(self, key, value, save=True):
        """! @brief Change a setting from code through its validator and change hook.
        @param save  persist now (False to batch several, then save_config()).
        @return the stored value.
        @throws ValueError when the validator rejects the value.
        """
        handled, err = self.config_registry.apply(key, value, self.config)
        if handled and err and not err.startswith("applied"):
            raise ValueError(f"{key}: {err}")
        if not handled:
            self.config[key] = value
        if save:
            self.save_config()
        return self.config.get(key)

    def persist_model_selection(self, save=True):
        """! @brief Persist the broker's current model picks (after broker.select)."""
        return self.set_config("model_selection", self.broker.current_selection(), save=save)

    def set_status(self, text):
        """! @brief Set the header status line (job progress; never persisted)."""
        self.config["status_text"] = str(text)

    def on_setting_change(self, key, fn):
        """! @brief Add a change handler to an already declared setting."""
        d = self.config_registry._settings.get(key)
        if d is not None:
            d["on_change"] = fn

    def add_settings_field(self, *, key, label, kind="text", pane="general",
                           tab=None, options=None, help=None, admin_only=False,
                           section=None, columns=None):
        """! @brief Add a settings field bound to a config key.
        @param kind     text | number | toggle | select | rows.
        @param pane     pane id ("general" or a module pane).
        @param tab      settings tab; defaults to the module's own tab, else General.
        @param options  select choices [{value, label}] or a callable returning them.
        @param section  a named spot inside the pane (General: "defaults", "system").
        @param columns  for "rows": [{key, label, placeholder?}].
        Saving needs write on the tab's permission.
        """
        self.settings_fields.append({
            "key": key, "label": label, "kind": kind, "pane": pane,
            "tab": tab, "options": options, "help": help,
            "admin_only": bool(admin_only), "section": section, "columns": columns,
            "module_id": self._current_module})

    def add_user_setting(self, key, *, label, kind="text", default=None, validate=None,
                         options=None, columns=None, feature=None, help=None, order=100):
        """! @brief Declare a per-user setting (Settings > User settings).
        @param kind      as add_settings_field.
        @param default   value, or fn(user) evaluated per request.
        @param validate  fn(value) -> cleaned value; raise ValueError to reject.
        @param feature   permission needed at write to change it (read shows it).
        Read it with user_setting(key).
        """
        self.user_settings[key] = {
            "key": key, "label": label, "kind": kind, "default": default,
            "validate": validate, "options": options, "columns": columns,
            "feature": feature, "help": help, "order": order,
            "module_id": self._current_module}

    def user_setting(self, key, username=None):
        """! @brief A user's value for a per-user setting, else its default."""
        return self.core.user_setting(key, username)

    def add_account_field(self, key, label, *, options=None, scopes=("user", "group"), help=None):
        """! @brief Add a field an admin sets per account and/or group in Settings > Users.
        A user's value beats the group's; read it from g.user["account"][key].
        @param options  [{value, label}] or a callable returning them ("" = unset).
        """
        return self.core.auth.register_account_field(key, label, options=options,
                                                      scopes=scopes, help=help)

    def update_file(self, target=None, **kw):
        """! @brief Write file metadata or a per-file DB row (core update_file).

            host.update_file(rel, add={"tags": ["cat"]})
            host.update_file(rel, remove={"regions": lambda r: r.get("debug")})
            host.update_file(rel, exif={"Rating": 4})
            host.update_file(rel, db={"face_done": 1}, dont_write=True)
            host.update_file(rel, table="image_embeddings", key={"model": m},
                             set={"vec": blob}, dont_write=True)

        dont_write=True keeps the change in the DB only; module tables require it.
        """
        return self.core.update_file(target, **kw)

    def register_metadata_writer(self, kind, fn, fields=(), claims=None):
        """! @brief Route update_file(set=...) for a media kind to this module's writer.
        @param fn      fn(rel, abs_path, fields, dont_write) -> falsy if the file is
                       unknown, else True or a dict of extra result keys.
        @param fields  field names the writer owns; the rest take the core path.
        @param claims  fn(rel) -> bool for files the extension doesn't place in `kind`.
        """
        self.core.register_metadata_writer(kind, fn, fields, claims)

    def provide_service(self, name, obj, priority=0):
        """! @brief Publish a service other modules can look up with get_service.
        @param priority  highest wins (ties: latest). A helper several modules ship a
                         copy of is published with its version, so the newest serves all.
        """
        cur = self.services.get(name)
        if cur is None or priority >= cur.get("priority", 0):
            self.services[name] = {"obj": obj, "module_id": self._current_module,
                                   "priority": priority}
        return name

    def get_service(self, name, default=None):
        """! @brief A service another module published.
        @return the service, or `default` when no enabled module provides it.
        """
        s = self.services.get(name)
        return s["obj"] if s else default

    def has_service(self, name):
        return name in self.services

    def register_feature(self, key, label, *, section="modules",
                         section_label="Modules", default="write",
                         role_defaults=None):
        """! @brief Register a permission feature this module owns.
        @param default        the level an "inherit" user gets.
        @param role_defaults  {role: level}, e.g. {"viewer": "block"}.
        """
        return self.core.features.register_feature(
            key, label, section=section, section_label=section_label,
            default=default, role_defaults=role_defaults)

    def require_feature(self, feature_key, action=None, fields=(), level="read"):
        """! @brief Decorator: require `level` on a feature for the current user."""
        return self.core.auth.require_feature(feature_key, action=action, fields=fields,
                                    level=level)

    def register_centre_pane(self, template):
        """! @brief Add a centre-pane partial (pair with registerMediaMode in JS)."""
        self.centre_panes.append({"template": template,
                                  "module_id": self._current_module})

    def register_app_modal(self, template):
        """! @brief Add a modal partial from this module's templates/, rendered into app.html."""
        self.app_modals.append({"template": template,
                                "module_id": self._current_module})

    def register_search_provider(self, fn):
        """! @brief Add non-image search results to the gallery.
        @param fn  fn(text, folder, structured) -> [entry], each with its own "kind".
        """
        self.search_providers.append(fn)

    def register_search_type(self, prefix, handler, *, help=None):
        """! @brief Handle a custom search token.
        @param prefix   token prefix with colon, e.g. "exif:".
        @param handler  fn(token, value) -> (sql_clause, params); ("", []) to ignore.
        @param help     one line for Settings > Info.
        """
        self.search_types[prefix] = handler
        self.search_help[prefix] = {"help": help or "", "module_id": self._current_module}

    def register_sort_key(self, name, expr, *, help=None):
        """! @brief Add a key for the `sort:` search token.
        @param name  the key, lowercase (`sort:name`, `sort:-name` for descending).
        @param expr  SQL ORDER BY expression over files, without parameters, or a
                     callable returning one (None skips it).
        """
        self.sort_keys[name.lower()] = expr
        names = ", ".join(sorted(self.sort_keys))
        self.search_help["sort:"] = {
            "help": f"sort:<key> or sort:-<key> (descending); keys: {names}"
                    + (f" - {help}" if help else ""),
            "module_id": self._current_module}

    def register_left_pane(self, template):
        """! @brief Add a left-column pane partial; pair it with a left tab of the same pane id."""
        self.left_panes.append({"template": template,
                                "module_id": self._current_module})

    def register_controls_pane(self, tab_id, template, *, feature=None):
        """! @brief Add a controls-pane partial, rendered server-side as #controls_pane_<tab_id>.
        @param template  template in this module's templates/ folder.
        @param feature   optional data-feature gate on the pane.
        """
        self.controls_panes.append({
            "tab_id": tab_id, "template": template, "feature": feature,
            "module_id": self._current_module})

    def add_worker_source(self, name, claim, handle, key_of=None, cost_of=None):
        """! @brief Register a thread-manager work source (see thread_manager.register_source)."""
        self.thread_manager.register_source(
            name, claim, handle, key_of=key_of, cost_of=cost_of)

    def add_background_sweep(self, cap_id, pending, run, batch=1):
        """! @brief Give a capability's "run in background" switch its work.
        @param pending  fn(db, n) -> up to n rel_paths still lacking this output.
        @param run      fn(rel, abs, handle) computes and stores it; with batch > 1,
                        fn(rels, abs_paths, handle).
        Detect / segment capabilities use the ingest path instead.
        """
        self.background_sweeps[cap_id] = {"pending": pending, "run": run,
                                          "batch": max(1, int(batch or 1)),
                                          "module_id": self._current_module}

    def _sweep_claim(self):
        caps = [c for c in self.broker.background_capabilities() if c in self.background_sweeps]
        if not caps:
            return None
        # A sweep never runs dry: cap it at half the pool so other work still gets slots.
        try:
            cap = max(1, int(self.thread_manager.max_slots()) // 2)
        except Exception:
            cap = 1
        if self._sweep_inflight >= cap:
            return None
        now = time.time()
        for _ in range(len(caps)):
            self._sweep_rr = (self._sweep_rr + 1) % len(caps)
            cap = caps[self._sweep_rr]
            if self._sweep_idle.get(cap, 0) > now:
                continue
            prov = self.broker.provider_for(cap, "bg")
            slot = self.thread_manager.try_acquire_model(prov.key) if prov is not None else ""
            if slot is None:
                continue
            want = self.background_sweeps[cap]["batch"]
            n = max(16, want)
            while True:
                try:
                    rows = list(self.background_sweeps[cap]["pending"](self.db(), n))
                except Exception as e:
                    self.logger.error(f"background {cap} pending: {e}"); rows = []
                rels, keys = [], []
                for rel in rows:
                    if (cap, rel) in self._sweep_skip:
                        continue
                    key = f"sweep:{cap}:{rel}"
                    if self.thread_manager.try_acquire_key(key):
                        rels.append(rel); keys.append(key)
                        if len(rels) >= want:
                            break
                if rels:
                    self._sweep_inflight += 1
                    return {"cap": cap, "rel_path": rels[0], "rel_paths": rels,
                            "key": keys[0], "keys": keys, "slot": slot}
                if len(rows) < n or n >= 512:
                    break
                n *= 4
            if slot:
                self.thread_manager.release_key(slot)
            self._sweep_idle[cap] = now + 60
        return None

    def _sweep_handle(self, job):
        cap = job["cap"]
        rels = job.get("rel_paths") or [job["rel_path"]]
        sweep = self.background_sweeps[cap]
        try:
            fps = [self.safe_path(self.media_dir, r) for r in rels]
            ok = [(r, f) for r, f in zip(rels, fps) if f and os.path.exists(f)]
            for r, f in zip(rels, fps):
                if not (f and os.path.exists(f)):
                    self._sweep_skip.add((cap, r))
            if not ok:
                return
            handle = self.broker.request(cap, "bg")
            if sweep["batch"] > 1:
                sweep["run"]([r for r, _ in ok], [f for _, f in ok], handle)
            else:
                sweep["run"](ok[0][0], ok[0][1], handle)
            n = self._sweep_done[cap] = self._sweep_done.get(cap, 0) + len(ok)
            if n // 25 != (n - len(ok)) // 25:
                self.set_status(f"[bg {cap}] {n} done...")
        except Exception as e:
            for r in rels:  # skip failures instead of retrying them every tick
                self._sweep_skip.add((cap, r))
            self.logger.error(f"background {cap} {rels[0]}{' +%d' % (len(rels) - 1) if len(rels) > 1 else ''}: {e}")
        finally:
            self._sweep_inflight = max(0, self._sweep_inflight - 1)
            for k in job.get("keys") or [job["key"]]:
                self.thread_manager.release_key(k)
            if job.get("slot"):
                self.thread_manager.release_model(job["slot"])
            self.thread_manager.wake()

    def _start_sweeps(self):
        if self.background_sweeps:
            self.thread_manager.register_source("background_sweep", self._sweep_claim,
                                                self._sweep_handle, key_of=lambda j: j["key"])

    def declare_capability(self, cap_id, *, summary, input, output, label=None,
                           background=False):
        """! @brief Declare a new capability contract (first declarer owns it).
        Only for capabilities the core doesn't declare already.
        """
        return self.broker.declare(cap_id, summary=summary, input=input,
                                   output=output, owner=self._current_module,
                                   label=label, background=background)

    def provide_model(self, cap_id, provider_id, *, label, loader,
                      transform=None, available=None, reason="",
                      cost_mb=0, gpu=False, handles=None, family=None,
                      sizes=None, types=None, settings=None, classes=None,
                      prompted=False, note="", speed="", supports_conf=None,
                      resource=None, concurrency=1):
        """! @brief Register this module's model as a provider of a capability.
        @param loader     fn() -> a callable model handle (cache it in model_registry).
        @param transform  fn(raw, *args) -> the capability's canonical output.
        @param available  fn() -> bool; unavailable providers show greyed out.
        @param handles    fn(model_path) -> bool, for path-parameterised capabilities.
        @param family     picker group label.
        @param sizes      size ids, e.g. ["n", "s", "m"].
        @param types      [{value, label}] variants.
        @param settings   extra provider widgets [{key, label, kind, options?, help?}];
                          each key must be declared with add_config_key.
        @param classes    fn() -> class names, for the background class whitelist.
        @param prompted   the handle takes (img, prompt); never a background pick.
        @param note       one line shown in the picker.
        @param speed      "fast" | "balanced" | "accurate".
        @param supports_conf  the handle accepts conf=0..1.
        @param resource   shared backend name; providers on it share `concurrency` jobs.
        """
        return self.broker.provide(
            cap_id, provider_id, label=label, loader=loader, transform=transform,
            available=available, reason=reason, cost_mb=cost_mb, gpu=gpu,
            handles=handles, family=family, sizes=sizes, types=types,
            settings=settings, classes=classes, prompted=prompted, note=note,
            speed=speed, supports_conf=supports_conf,
            resource=resource, concurrency=concurrency,
            module_id=self._current_module)

    def model_variant(self, cap_id, role=None, provider=None):
        """! @brief The user's picks for a capability: {"size", "type", "background", "classes"}.
        Called by providers inside their loader.
        """
        return self.broker.variant(cap_id, role, provider)

    def request_model(self, cap_id, role="fg", provider=None):
        """! @brief A ready handle for the selected provider of a capability.
        @throws broker.NoProviderError when nothing can serve it.
        """
        return self.broker.request(cap_id, role, provider)

    def register_authenticator(self, fn):
        """! @brief Add a way to authenticate a request besides the session cookie.
        @param fn  fn() -> None (not mine), False (mine, refused) or (user, info);
                   info is stored on g.api_key.
        """
        self.core.authmgr.authenticators.append(fn)
        
    def register_access_policy(self, policy):
        """! @brief Add a per-request access policy (all methods optional):

            files_clause(column) -> (clauses, params)   limit file rows
            check_path(rel_path, write) -> bool
            albums_clause(alias) -> (clauses, params)
            album_level(name) -> "owner" | "write" | "read" | None
            album_info(name) -> dict                    merged into /api/albums
            album_event(event, **kw)                    created / deleted / renamed
            upload_folder(folder, form) -> folder

        The most restrictive answer of all policies wins; outside a request nothing
        is restricted.
        """
        self.access_policies.append(policy)

    def files_clause(self, column="rel_path"):
        clauses, params = [], []
        for pol in self.access_policies:
            fn = getattr(pol, "files_clause", None)
            if fn:
                c, p = fn(column)
                clauses += c; params += p
        return clauses, params

    def albums_clause(self, alias="a"):
        clauses, params = [], []
        for pol in self.access_policies:
            fn = getattr(pol, "albums_clause", None)
            if fn:
                c, p = fn(alias)
                clauses += c; params += p
        return clauses, params

    def check_path(self, rel_path, write=False):
        return all(fn(rel_path, write) for fn in
                   (getattr(pol, "check_path", None) for pol in self.access_policies) if fn)

    _LEVEL_RANK = {None: 0, "read": 1, "write": 2, "owner": 3}

    def album_level(self, name):
        """! @brief The viewer's level on an album: the lowest any policy gives ("owner" if none)."""
        level = "owner"
        for pol in self.access_policies:
            fn = getattr(pol, "album_level", None)
            if fn:
                lv = fn(name)
                if self._LEVEL_RANK[lv] < self._LEVEL_RANK[level]:
                    level = lv
        return level

    def album_info(self, name):
        out = {}
        for pol in self.access_policies:
            fn = getattr(pol, "album_info", None)
            if fn:
                out.update(fn(name) or {})
        return out

    def album_event(self, event, **kw):
        for pol in self.access_policies:
            fn = getattr(pol, "album_event", None)
            if fn:
                fn(event, **kw)

    def upload_folder(self, folder, form):
        for pol in self.access_policies:
            fn = getattr(pol, "upload_folder", None)
            if fn:
                folder = fn(folder, form)
        return folder

    def register_gallery_filter(self, clause):
        """! @brief Hide matching rows from the flat gallery (a SQL condition on files)."""
        self.gallery_filters.append(clause)

    def register_ai_actions(self, source, list_fn, run_fn, *, feature=None):
        """! @brief Add actions to the editor's AI picker and the bulk bar.
        @param source   contributor id, e.g. "vlm".
        @param list_fn  fn() -> [{"id", "label", "target"}]; target is what the
                        action produces (description, tags, regions, flag, ...).
        @param run_fn   fn(action_id, fp, bgr, meta) -> {regions, tags, description,
                        flag, note} (any of them).
        @param feature  permission gating these actions (default ai_tooling).
        """
        self.ai_action_groups.append({"source": source, "list": list_fn, "run": run_fn,
                                      "feature": feature, "module_id": self._current_module})

    def register_action_target(self, name, fn):
        """! @brief Add an AI-action target.
        @param fn  fn(fp, bgr, meta, action) -> the regions added, or True.
        """
        self.action_targets[name] = fn

    def register_pipeline_stage(self, name, fn, *, label=None, editor=None):
        """! @brief Add a stage the AI pipeline can run.
        @param name    node type in the pipeline tree.
        @param fn      fn(image_bgr) -> the stage result.
        @param editor  hints for the pipeline editor (has_prompt, has_store, ...).
        A disabled module's stage is simply absent and its nodes do nothing.
        """
        self.pipeline_stages[name] = {
            "fn": fn, "label": label or name,
            "editor": editor or {}, "module_id": self._current_module}
        return name

    def add_table(self, ddl, *, check=None):
        """! @brief Declare a table this module owns.
        @param ddl    CREATE TABLE IF NOT EXISTS statement(s).
        @param check  fn(db) run once at startup to repair a cache table that drifted
                      from its source of truth.
        """
        self.db_tables.append({"ddl": ddl, "check": check,
                               "module_id": self._current_module})

    def register_file_enricher(self, fn):
        """! @brief Add per-file fields to gallery / list rows.
        @param fn  fn(db, rel_paths) -> {rel_path: {field: value}}; one query per call.
        """
        self.file_enrichers.append({"fn": fn, "module_id": self._current_module})

    def enrich_file_rows(self, db, rows, path_key="filename"):
        """! @brief Merge every enricher's fields into `rows` (in place).
        @param path_key  the row key holding the rel_path.
        @return rows.
        """
        if not rows or not self.file_enrichers:
            return rows
        paths = [r.get(path_key) for r in rows if r.get(path_key)]
        for e in self.file_enrichers:
            try:
                extra = e["fn"](db, paths) or {}
            except Exception as ex:
                self.logger.error(
                    f"module '{e['module_id']}' file enricher failed: {ex}")
                continue
            for r in rows:
                add = extra.get(r.get(path_key))
                if add:
                    r.update(add)
        return rows

    def on_startup(self, fn):
        """! @brief Run fn() once after the server is up (not at import time)."""
        self.startup_hooks.append(fn)

    def register_media_type(self, kind, **spec):
        """! @brief Register a new file kind (extensions, mime, flags) with the media registry."""
        return self.media.register_media_type(kind, **spec)

    def extend_media_type(self, kind, **spec):
        """! @brief Add extensions or mime types to a kind another module owns."""
        return self.media.extend_media_type(kind, **spec)

    def add_public_prefix(self, prefix):
        """! @brief Let a URL prefix through the login gate (machine-to-machine calls).
        Routes under it must check their own credential on every request.
        """
        return self.core.auth.add_public_prefix(prefix)

    def current_user(self):
        """! @brief The signed-in username, or ''."""
        from_g = getattr(self.core, "current_user", None)
        return from_g() if from_g else ""

    def on(self, event, fn):
        """! @brief Subscribe fn(**kw) to a core event, e.g. library.reconcile,
        upload.duplicate_check(sha, filename), upload.stored(rel_path, filename),
        file.renamed(old_rel, new_rel), file.deleted(rel_path),
        file.metadata_changed(rel_path, abs_path, fields).
        """
        self.event_hooks.setdefault(event, []).append(fn)

    def emit(self, event, **kw):
        """! @brief Call every subscriber of `event`; a failing one is logged and skipped.
        @return the non-None results.
        """
        out = []
        for fn in self.event_hooks.get(event, []):
            try:
                r = fn(**kw)
            except Exception as e:
                self.logger.error(f"event {event} handler failed: {e}")
                continue
            if r is not None:
                out.append(r)
        return out

    def run_startup_hooks(self):
        for fn in self.startup_hooks:
            try:
                fn()
            except Exception as e:
                self.logger.error(f"module startup hook failed: {e}")
        self._start_sweeps()

    def apply_db_tables(self, db):
        """! @brief Create module tables and run their checks; failures are logged, never raised."""
        for t in self.db_tables:
            try:
                db.executescript(t["ddl"])
                db.commit()
            except Exception as e:
                self.logger.error(
                    f"module '{t['module_id']}' add_table failed: {e}")
                continue
            if t["check"]:
                try:
                    t["check"](db)
                except Exception as e:
                    self.logger.error(
                        f"module '{t['module_id']}' table check failed: {e}")
