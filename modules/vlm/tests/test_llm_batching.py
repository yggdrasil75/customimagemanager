"""! @file
@brief Pipeline runners and LLM throughput: a node's `verify` reaches the llm
callable (and only when set), legacy trees keep it when converted, and one
endpoint with N parallel slots runs a photo's subjects N at a time."""
import threading
import time

import numpy as np

from modules.pipeline import graph_engine
from modules.pipeline import engine
from modules.pipeline import pipeline_core as pc

IMG = np.zeros((64, 64, 3), np.uint8)


def _graph(verify=None):
    n = {"id": "q", "type": "llm", "want": "bool", "prompt": "animal?", "in": {"image": ["start", "image"]}}
    if verify:
        n["verify"] = verify
    return {"schema": "graph/1", "nodes": [{"id": "start", "type": "start"}, n,
                                           {"id": "end", "type": "end", "in": {}}]}


def test_node_verify_reaches_the_llm_only_when_set():
    got = []

    def llm(prompt, img, want, choices=None, endpoint=None, **kw):
        got.append(kw)
        return True
    graph_engine.run_graph(_graph("always"), IMG, llm, endpoints=["http://a"], collect=["q"])
    graph_engine.run_graph(_graph("default"), IMG, llm, endpoints=["http://a"], collect=["q"])
    graph_engine.run_graph(_graph(), IMG, llm, endpoints=["http://a"], collect=["q"])
    assert got == [{"verify_mode": "always"}, {}, {}]


def test_plain_llm_callable_still_works_with_verify_set():
    def llm(prompt, img, want, choices=None):        # no endpoint, no kwargs
        return True
    out = graph_engine.run_graph(_graph("always"), IMG, llm, endpoints=["http://a"],
                                 collect=["q"])
    assert out["_outputs"]["q"]["result"] is True


def test_legacy_tree_step_verify_and_conversion():
    tree = {"start": "d", "nodes": [
        {"id": "d", "type": "llm", "want": "text", "store": "summary", "prompt": "describe", "verify": "always",
         "next": None}]}
    got = []

    def llm(prompt, img, want, choices=None, endpoint=None, **kw):
        got.append(kw)
        return "x"
    engine.run_pipeline(tree, IMG, llm)
    assert got == [{"verify_mode": "always"}]
    g = graph_engine.tree_to_graph(engine.DEFAULT_PIPELINE)
    assert all("verify" not in n for n in g["nodes"])
    g = graph_engine.tree_to_graph(tree)
    assert next(n for n in g["nodes"] if n["id"] == "d")["verify"] == "always"


def test_one_endpoint_with_slots_describes_subjects_together(monkeypatch):
    monkeypatch.setattr(pc, "state", {"oai_endpoint": "http://one/v1", "oai_concurrency": 4})
    eps = pc._pipeline_endpoints()
    assert eps == ["http://one/v1"] * 4 and pc._llm_workers() == 4
    monkeypatch.setattr(pc, "state", {"oai_endpoint": "http://one/v1", "oai_concurrency": 1})
    assert pc._pipeline_endpoints() == ["http://one/v1"]

    lock, now, peak = threading.Lock(), [0], [0]

    def llm(prompt, img, want, choices=None, endpoint=None, **kw):
        with lock:
            now[0] += 1
            peak[0] = max(peak[0], now[0])
        time.sleep(0.03)
        with lock:
            now[0] -= 1
        return [{"cx": .2 + .15 * i, "cy": .5, "w": .1, "h": .2, "class_name": "p"} for i in range(4)] \
            if want == "boxes" else "d"
    graph = {"schema": "graph/1", "nodes": [
        {"id": "start", "type": "start"},
        {"id": "bx", "type": "boxes", "prompt": "people", "in": {"image": ["start", "image"]}},
        {"id": "each", "type": "for_each", "in": {"image": ["start", "image"], "items": ["bx", "subjects"]},
         "graph": {"nodes": [{"id": "item", "type": "_inner_start"},
                             {"id": "s", "type": "llm", "want": "text", "prompt": "describe",
                              "in": {"image": ["item", "crop"]}},
                             {"id": "item_end", "type": "_inner_end", "in": {"description": ["s", "result"]}}]}},
        {"id": "end", "type": "end", "in": {"subjects": [["each", "items"]]}}]}
    out = graph_engine.run_graph(graph, IMG, llm, endpoints=eps)
    assert len(out["subjects"]) == 4
    assert peak[0] == 4


def test_fan_out_keeps_order_and_parallelism(monkeypatch):
    monkeypatch.setattr(pc, "state", {"oai_endpoint": "http://one/v1", "oai_concurrency": 3})
    monkeypatch.setattr(pc, "HOST", None)
    lock, now, peak = threading.Lock(), [0], [0]

    def f(x):
        with lock:
            now[0] += 1
            peak[0] = max(peak[0], now[0])
        time.sleep(0.02)
        with lock:
            now[0] -= 1
        return x * 2
    assert pc.fan_out(f, range(9)) == [x * 2 for x in range(9)]
    assert peak[0] == 3