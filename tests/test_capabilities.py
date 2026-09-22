"""Machine-capability gate (modules/capabilities) vs. the model broker.

The gate answers 503 "feature unavailable on this server" for a feature whose
pip package is missing. The broker knows whether a model for that feature is
actually available. When the two disagree, a working feature is locked out.
These tests report every such lock-out on the current machine."""
import pytest
from cimtest import load_app

# feature key -> broker capabilities it needs (all of them)
FEATURE_CAPS = {
    "ai.pose":     ["pose"],
    "ai.segment":  ["segment"],
    "ai.autotag":  ["detect"],
    "ai.ocr":      ["ocr"],
    "ai.barcodes": ["detect.barcodes"],
    "ai.iqa":      ["iqa"],
    "tab.faces":   ["detect.faces", "embed.faces"],
}


def _local_available(b, cap):
    """Providers that run on this machine (endpoint-backed ones excluded:
    their availability only means 'configured')."""
    return [pid for pid, p in b._providers.get(cap, {}).items()
            if p.available() and not p.resource and pid != "cim_test_fake"]


@pytest.mark.parametrize("feature", sorted(FEATURE_CAPS))
def test_gate_agrees_with_broker(feature):
    from modules.capabilities import capabilities
    app = load_app()
    b = app.module_host.broker
    denied = capabilities.capability_denials().get(feature) is False
    avail = {c: _local_available(b, c) for c in FEATURE_CAPS[feature]}
    usable = all(avail.values())          # every listed capability must be served
    if denied and usable:
        need = [c for c, keys in capabilities.CAPABILITY_FEATURES.items() if feature in keys]
        pytest.fail(f"'{feature}' is gated off (needs pip: {need}) but the broker has working "
                    f"models for it: {avail}")


def test_probe_is_consistent():
    from modules.capabilities import capabilities
    caps = capabilities.probe()
    assert set(caps) == set(capabilities.CAPABILITY_PROBES)
    for cap, keys in capabilities.CAPABILITY_FEATURES.items():
        assert cap in capabilities.CAPABILITY_PROBES, f"feature map names unknown probe {cap}"
