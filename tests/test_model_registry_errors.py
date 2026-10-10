"""! @file
@brief A model that fails to load says why: the cause is logged once and kept for
the provider's error message, instead of a bare "failed to load"."""
import logging

import model_registry


def test_failed_load_keeps_and_logs_the_cause(caplog):
    key = "test:broken-model"

    def boom():
        raise ValueError("Unrecognized model type 'embedding_gemma2'")
    model_registry.register(key, boom, cost_mb=0, gpu=False)
    caplog.set_level(logging.ERROR, logger="access")
    try:
        assert model_registry.acquire(key) is None
        assert model_registry.error(key) == "ValueError: Unrecognized model type 'embedding_gemma2'"
        assert "test:broken-model failed to load: ValueError" in caplog.text
        assert model_registry.error("test:never-registered") == ""
    finally:
        model_registry.unload(key)
