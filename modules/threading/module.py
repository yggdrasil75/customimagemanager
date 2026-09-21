"""
Thread Manager module — registration side of modules/threading.
======================================================================
The core still constructs the ThreadManager itself (manager.py imports it
first, before any plugin loads); this register(host) is where the module
declares what it OWNS: its settings and its wiring to the model broker.
Called by manager.py right after the host exists, like modules.metadata.
"""


def register(host):
    tm = host.thread_manager
    # The broker signs every provider up here (what it runs on, what it
    # costs); jobs are admitted against the device budget (try_acquire_model).
    host.broker.thread_manager = tm
    for cap_id, provs in getattr(host.broker, "_providers", {}).items():   # providers registered before wiring
        for p in provs.values():
            tm.register_model(p.key, gpu=p.gpu, resource=p.resource,
                              concurrency=p.concurrency, cost_mb=p.cost_mb)

    # Optional ceiling on concurrent background GPU jobs. Admission is by
    # memory (each job reserves a working set sized from its model against
    # the device budget), so this is for taming a card that thrashes, not
    # the normal control: 0 = memory alone.
    host.add_config_key("gpu_max_jobs", default=0,
                        validate=lambda v: max(0, min(256, int(v or 0))))
    host.add_settings_field(key="gpu_max_jobs", label="Max background GPU jobs (0 = by memory)",
                            kind="number", pane="general",
                            help="Background model jobs are admitted by device memory: small "
                                 "models run many at once, a 16 GB embedder one or two. Set a "
                                 "number only to cap that (e.g. if you see 'NMS time limit exceeded').")
    tm.set_gpu_max_jobs(lambda: host.config.get("gpu_max_jobs") or 0)