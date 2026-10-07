"""! @file
@brief Thread manager registration: its settings and its link to the model broker.
manager.py builds the ThreadManager first and calls this right after the host exists.
"""


def register(host):
    tm = host.thread_manager
    # the broker signs each provider up for job admission
    host.broker.thread_manager = tm
    for cap_id, provs in getattr(host.broker, "_providers", {}).items():  # providers registered before this
        for p in provs.values():
            tm.register_model(p.key, gpu=p.gpu, resource=p.resource,
                              concurrency=p.concurrency, cost_mb=p.cost_mb)

    # Ceiling on concurrent background GPU jobs (0 = admit by memory alone).
    host.add_config_key("gpu_max_jobs", default=0,
                        validate=lambda v: max(0, min(256, int(v or 0))))
    host.add_settings_field(key="gpu_max_jobs", label="GPU jobs (0 = auto)",
                            kind="number", pane="general", section="system",
                            help="Background model jobs are admitted by device memory: small "
                                 "models run many at once, a 16 GB embedder one or two. Set a "
                                 "number only to cap that (e.g. if you see 'NMS time limit exceeded').")
    tm.set_gpu_max_jobs(lambda: host.config.get("gpu_max_jobs") or 0)