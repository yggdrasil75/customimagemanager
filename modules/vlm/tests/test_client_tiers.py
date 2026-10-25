"""! @file
@brief vlm client without a server: the image goes first, answers are flagged,
the confirming model checks them (OK or a fix), and the in-flight cap holds
requests to a server's slot count while keeping that many in flight."""
import contextlib
import threading
import time

import numpy as np
import pytest

from modules.vlm import client


class FakeTM:
    def __init__(self, worker=True):
        self.worker = worker

    def in_worker(self):
        return self.worker

    @contextlib.contextmanager
    def foreground_use(self, resource):
        yield


class FakeHost:
    def __init__(self, **cfg):
        self.config = {"oai_endpoint": "http://main:5001/v1", "oai_model": "small", "oai_key": "k-main",
                       "oai_system_prompt": "sys", "oai_concurrency": 1, "oai_verify_mode": "off",
                       "oai_verify_endpoint": "", "oai_verify_model": "", "oai_verify_key": "",
                       "oai_verify_concurrency": 1, "oai_verify_min_prob": 0}
        self.config.update(cfg)
        self.thread_manager = FakeTM()
        self.logger = None

    def emit(self, name, **kw):
        return []


class Resp:
    def __init__(self, choice):
        self.status_code = 200
        self._j = {"choices": [choice]}

    def raise_for_status(self):
        pass

    def json(self):
        return self._j


def _reply(text=None, finish="stop", tool_args=None, logprob=None):
    msg = {"content": text}
    if tool_args is not None:
        msg["tool_calls"] = [{"function": {"arguments": tool_args}}]
    ch = {"message": msg, "finish_reason": finish}
    if logprob is not None:
        ch["logprobs"] = {"content": [{"logprob": logprob}]}
    return ch


@pytest.fixture
def fake(monkeypatch):
    """! @brief Installs a host and a scripted requests.post; returns (host, sent, script).
    script maps model -> list of replies (popped in order)."""
    host = FakeHost()
    monkeypatch.setattr(client, "HOST", host)
    sent, script = [], {}

    def post(url, json=None, headers=None, timeout=None):
        sent.append({"url": url, "json": json, "headers": headers})
        return Resp(script[json["model"]].pop(0))
    monkeypatch.setattr(client.requests, "post", post)
    return host, sent, script


IMG = np.zeros((8, 8, 3), np.uint8)


def test_image_goes_before_the_question(fake):
    host, sent, script = fake
    script["small"] = [_reply("a cat")]
    assert client.call("describe", IMG, "text") == "a cat"
    content = sent[0]["json"]["messages"][1]["content"]
    assert content[0]["type"] == "image_url" and content[1] == {"type": "text", "text": "describe"}
    assert sent[0]["json"]["messages"][0] == {"role": "system", "content": "sys"}


def test_values_unchanged_and_flags(fake):
    ok = lambda w, t, ch=None: client.parse(w, {"content": t}, ch)
    assert ok("bool", "Yes.") == (True, [])
    assert ok("bool", "Maybe, it looks like one")[1] == ["unclear yes/no"]
    assert ok("choice", "Face", ["Face", "Pet"]) == ("Face", [])
    assert ok("choice", "a face or a pet", ["Face", "Pet"]) == ("Face", ["several choices"])
    assert ok("choice", "a dog", ["Face", "Pet"]) == ("Face", ["no choice"])
    assert ok("tags", "1girl, red hair") == (["1girl", "red hair"], [])
    assert "sentence in tags" in ok("tags", "the girl is standing in a field of flowers today")[1]
    assert ok("json", '{"a": 1}') == ({"a": 1}, [])
    assert ok("json", "no idea") == ({}, ["malformed"])
    assert ok("text", "")[1] == ["empty"]
    boxes, flags = client.parse("boxes", {"tool_calls": [{"function": {"arguments": "{bad"}}]})
    assert boxes == [] and flags == ["malformed"]


def test_off_by_default_no_second_call(fake):
    host, sent, script = fake
    script["small"] = [_reply("Maybe")]
    host.config.update(oai_verify_model="big")
    client.call("animal?", IMG, "bool")
    assert len(sent) == 1


def test_flagged_answer_is_confirmed_and_ok_keeps_it(fake):
    host, sent, script = fake
    host.config.update(oai_verify_mode="flagged", oai_verify_model="big")
    script["small"] = [_reply("Probably a dog")]
    script["big"] = [_reply("OK")]
    assert client.call("animal?", IMG, "bool") is False      # the draft's value stands
    assert [s["json"]["model"] for s in sent] == ["small", "big"]
    v = sent[1]["json"]["messages"][1]["content"]
    assert v[0]["type"] == "image_url" and v[0] == sent[0]["json"]["messages"][1]["content"][0]
    assert "Probably" not in v[1]["text"] and "no" in v[1]["text"] and "unclear yes/no" in v[1]["text"]
    # same server, blank verify endpoint: the main key is reused
    assert sent[1]["url"] == "http://main:5001/v1/chat/completions"
    assert sent[1]["headers"]["Authorization"] == "Bearer k-main"


def test_clean_answer_is_not_confirmed_in_flagged_mode(fake):
    host, sent, script = fake
    host.config.update(oai_verify_mode="flagged", oai_verify_model="big")
    script["small"] = [_reply("Pet")]
    assert client.call("kind?", IMG, "choice", ["Face", "Pet"]) == "Pet"
    assert len(sent) == 1


def test_correction_replaces_the_draft(fake):
    host, sent, script = fake
    host.config.update(oai_verify_mode="always", oai_verify_model="big")
    script["small"] = [_reply("1girl, sword")]
    script["big"] = [_reply("1girl, spear, armor")]
    assert client.call("tags", IMG, "tags") == ["1girl", "spear", "armor"]


def test_cut_off_answer_is_flagged(fake):
    host, sent, script = fake
    host.config.update(oai_verify_mode="flagged", oai_verify_model="big")
    script["small"] = [_reply("A long description that never", finish="length")]
    script["big"] = [_reply("A long description that ends.")]
    assert client.call("describe", IMG, "text") == "A long description that ends."
    assert "cut off" in sent[1]["json"]["messages"][1]["content"][1]["text"]


def test_low_probability_is_flagged_when_enabled(fake):
    host, sent, script = fake
    host.config.update(oai_verify_mode="flagged", oai_verify_model="big", oai_verify_min_prob=0.8)
    script["small"] = [_reply("Yes", logprob=float(np.log(0.55)))]
    script["big"] = [_reply("No")]
    assert client.call("animal?", IMG, "bool") is False
    assert sent[0]["json"]["logprobs"] is True


def test_other_host_never_gets_the_main_key(fake):
    host, sent, script = fake
    host.config.update(oai_verify_mode="always", oai_verify_model="big",
                       oai_verify_endpoint="https://api.example.com/v1")
    script["small"] = [_reply("x")]
    script["big"] = [_reply("OK")]
    client.call("q", IMG, "text")
    assert sent[1]["url"] == "https://api.example.com/v1/chat/completions"
    assert "Authorization" not in sent[1]["headers"]


def test_verify_failure_keeps_the_draft(fake, monkeypatch):
    host, sent, script = fake
    host.config.update(oai_verify_mode="always", oai_verify_model="big")
    script["small"] = [_reply('{"keep": true}')]
    script["big"] = []                                    # pop from empty -> the call raises
    assert client.call("judge", IMG, "json") == {"keep": True}


def test_node_override_beats_the_setting(fake):
    host, sent, script = fake
    host.config.update(oai_verify_mode="off", oai_verify_model="big")
    script["small"] = [_reply("Pet")]
    script["big"] = [_reply("OK")]
    client.call("kind?", IMG, "choice", ["Face", "Pet"], verify_mode="always")
    assert len(sent) == 2


def test_gate_keeps_n_in_flight(monkeypatch):
    host = FakeHost(oai_concurrency=3)
    monkeypatch.setattr(client, "HOST", host)
    lock, now, peak = threading.Lock(), [0], [0]

    def post(url, json=None, headers=None, timeout=None):
        with lock:
            now[0] += 1
            peak[0] = max(peak[0], now[0])
        time.sleep(0.05)
        with lock:
            now[0] -= 1
        return Resp(_reply("x"))
    monkeypatch.setattr(client.requests, "post", post)
    ts = [threading.Thread(target=client.call, args=("q", None, "text")) for _ in range(12)]
    t0 = time.time()
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert peak[0] == 3                       # never more than the slots, and all of them used
    assert time.time() - t0 < 12 * 0.05 * 0.6  # ran in parallel, not one after another