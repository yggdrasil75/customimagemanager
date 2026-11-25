"""! @file
@brief Library chat: the /chat and /chatk proxies, the tool loop and the proposal queue,
against a fake koboldcpp (a local HTTP server scripted per test). Auth is off in the
test app; `as_` runs requests as real non-admin accounts with chosen roles."""
import base64
import io
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PIL import Image

import features
from cimtest import signed_in_as
from modules.kobold_chat.proxy import Proxy
from modules.kobold_chat.tools import ToolError, clean_rel

LCPP_HTML = (b'<!doctype html><html><head><meta charset="utf-8"></head><body>'
             b'<script>__sveltekit_x = {base: "", assets: "/lcpp"};</script></body></html>')
LITE_HTML = b"<!doctype html><html><HEAD><title>Lite</title></HEAD><body>lite</body></html>"


class FakeKobold:
    """! @brief A scripted koboldcpp: records chat requests, answers from `replies`."""

    def __init__(self):
        """! @brief Start the server on a free port."""
        self.requests, self.replies, self.hits = [], [], []
        fake = self

        class H(BaseHTTPRequestHandler):
            """! @brief The fake's request handler."""
            def log_message(self, *a):
                """! @brief Quiet."""
                pass

            def _send(self, code, body, ctype="application/json"):
                """! @brief One complete reply."""
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                """! @brief The pages, the version probe and /props."""
                fake.hits.append(("GET", self.path, dict(self.headers)))
                if self.path == "/lcpp/":
                    return self._send(200, LCPP_HTML, "text/html")
                if self.path == "/":
                    return self._send(200, LITE_HTML, "text/html")
                path = self.path[5:] if self.path.startswith("/lcpp/") else self.path
                if path == "/api/extra/version":
                    return self._send(200, json.dumps({"result": "KoboldCpp", "version": "1.120",
                                                       "mcp": True, "savedata": True,
                                                       "websearch": True}).encode())
                if path == "/props":
                    return self._send(200, b'{"n_ctx": 8192}')
                return self._send(404, b'{"error":"nope"}')

            def do_POST(self):
                """! @brief Chat completions from the script (streamed when asked)."""
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                fake.hits.append(("POST", self.path, dict(self.headers)))
                fake.requests.append(body)
                reply = fake.replies.pop(0) if fake.replies else {"content": "done"}
                if body.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for ch in reply.get("chunks") or []:
                        self.wfile.write(b"data: " + json.dumps(ch).encode() + b"\n\n")
                    self.wfile.write(b"data: [DONE]\n\n")
                    return
                msg = {"role": "assistant", "content": reply.get("content")}
                if reply.get("tool_calls"):
                    msg["tool_calls"] = reply["tool_calls"]
                self._send(200, json.dumps({"id": "x", "object": "chat.completion", "choices": [
                    {"index": 0, "message": msg,
                     "finish_reason": "tool_calls" if reply.get("tool_calls") else "stop"}]}).encode())

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"


@pytest.fixture
def kobold(host):
    """! @brief Point the kobold service at a fresh FakeKobold for one test."""
    svc = host.get_service("kobold")
    fake = FakeKobold()
    saved = dict(svc)
    svc["url"] = lambda path="": fake.url + path
    svc["running"] = lambda: True
    yield fake
    svc.clear()
    svc.update(saved)
    fake.server.shutdown()


@pytest.fixture(scope="module")
def accounts(client):
    """! @brief Two real non-admin accounts, removed afterwards."""
    out = {}
    for name in ("kc_alice", "kc_bob"):
        r = client.post("/api/auth/users/create", json={"username": name, "password": "pw", "role": "custom"})
        assert r.status_code == 200, r.get_json()
        out[name] = r.get_json()["user"]
    yield out
    for u in out.values():
        client.post("/api/auth/users/delete", json={"id": u["id"]})


def as_(app, acct, role="custom", **overrides):
    """! @brief Requests run as this account, with the role's permissions."""
    return signed_in_as(app, {**acct, "is_admin": False, "role": role, "effective_role": role,
                              "features": features.effective_permissions(role, overrides)})


def tool_call(name, args, cid="c1"):
    """! @brief An OpenAI tool call as the model would send it."""
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def test_pages_are_proxied_with_the_shim(client, kobold):
    r = client.get("/chat/")
    assert r.status_code == 200
    html = r.get_data()
    assert b"/chat/_cim/session.js" in html and b"/modules/kobold_chat/static/chat_shim.js" in html
    assert b'assets: "/chat"' in html and b'"/lcpp"' not in html
    assert html.index(b"session.js") < html.index(b"<body>")
    lite = client.get("/chatk/").get_data()
    assert b"/chatk/_cim/session.js" in lite and lite.index(b"<HEAD>") < lite.index(b"session.js")
    assert client.get("/chat").status_code in (301, 302, 308)
    js = client.get("/chat/_cim/session.js").get_data(as_text=True)
    assert js.startswith("window.CIM_CHAT = ") and '"prefix": "/chat"' in js
    assert client.get("/modules/kobold_chat/static/chat_shim.js").status_code == 200


def test_browser_credentials_are_not_forwarded(client, kobold):
    client.set_cookie("cim_session", "secret-cookie")
    assert client.get("/chat/props", headers={"Authorization": "Bearer x"}).status_code == 200
    _m, path, headers = kobold.hits[-1]
    assert path == "/lcpp/props"
    low = {k.lower() for k in headers}
    assert "cookie" not in low and "authorization" not in low
    client.delete_cookie("cim_session")


@pytest.mark.parametrize("path", ["/chat/cors-proxy", "/chat/mcp", "/chatk/mcp", "/chatk/api/extra/shutdown",
                                  "/chatk/api/extra/data/list", "/chatk/api/extra/multiplayer/status",
                                  "/chatk/api/extra/websearch", "/chat/api/extra/data/save"])
def test_server_wide_endpoints_are_refused(client, kobold, path):
    before = len(kobold.hits)
    assert client.post(path, json={}).status_code in (403, 404)
    assert len(kobold.hits) == before


def test_admin_only_endpoints(app, client, kobold, accounts):
    with as_(app, accounts["kc_alice"]):
        assert client.post("/chat/models/load", json={}).status_code == 403


def test_lite_version_hides_shared_extras(client, kobold):
    j = client.get("/chatk/api/extra/version").get_json()
    assert j["version"] == "1.120" and j["savedata"] is False and j["websearch"] is False
    assert j["mcp"] is False


def test_uploaders_are_refused(app, client, kobold, accounts):
    with as_(app, accounts["kc_bob"], role="uploader", **{"chat.use": "read"}):
        assert client.get("/chat/").status_code == 403
        assert client.post("/chat/v1/chat/completions", json={"messages": []}).status_code == 403
        assert client.get("/api/kobold_chat/actions").status_code == 403
    assert not kobold.requests


def test_viewers_get_read_tools_only(app, client, kobold, accounts):
    with as_(app, accounts["kc_bob"], role="viewer"):
        r = client.post("/chat/v1/chat/completions",
                        json={"messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200, r.get_data(as_text=True)
    names = {t["function"]["name"] for t in kobold.requests[0]["tools"]}
    assert {"search_library", "view_image", "read_text"} <= names
    assert not {n for n in names if n.startswith("propose_")}
    assert "upload_link" not in names
    assert kobold.requests[0]["messages"][0]["role"] == "system"


def test_tool_loop_runs_tools_as_the_user(app, client, kobold, accounts, upload):
    with as_(app, accounts["kc_alice"], **{"data.upload": "write"}):
        mine = upload("kcmine_secret.png", seed=31)
    assert mine.startswith("users/kc_alice/")
    # bob asks; the search must not find alice's personal file
    kobold.replies = [{"tool_calls": [tool_call("search_library", {"text": "kcmine_secret"})]},
                      {"content": "nothing"}]
    with as_(app, accounts["kc_bob"]):
        j = client.post("/chat/v1/chat/completions",
                        json={"messages": [{"role": "user", "content": "find it"}]}).get_json()
    assert j["choices"][0]["message"]["content"] == "nothing"
    tool_msg = [m for m in kobold.requests[1]["messages"] if m["role"] == "tool"][0]
    assert json.loads(tool_msg["content"])["results"] == []
    assert "[tool] search_library" in j["choices"][0]["message"]["reasoning_content"]
    # alice asks the same and finds it, and can look at it
    kobold.requests.clear()
    kobold.replies = [{"tool_calls": [tool_call("search_library", {"text": "kcmine_secret"}),
                                      tool_call("view_image", {"path": mine}, "c2")]},
                      {"content": "found"}]
    with as_(app, accounts["kc_alice"]):
        j = client.post("/chat/v1/chat/completions",
                        json={"messages": [{"role": "user", "content": "find it"}]}).get_json()
    assert j["choices"][0]["message"]["content"] == "found"
    tools = [m for m in kobold.requests[1]["messages"] if m["role"] == "tool"]
    assert json.loads(tools[0]["content"])["results"][0]["path"] == mine
    assert tools[1]["content"].startswith("{"), tools[1]["content"]
    img = json.loads(tools[1]["content"])
    assert img["content"][1]["type"] == "image" and img["content"][1]["mimeType"] == "image/jpeg"
    # bob cannot view it by name either
    with as_(app, accounts["kc_bob"]):
        kobold.requests.clear()
        kobold.replies = [{"tool_calls": [tool_call("view_image", {"path": mine})]}, {"content": "x"}]
        client.post("/chat/v1/chat/completions", json={"messages": [{"role": "user", "content": "look"}]})
    tool_msg = [m for m in kobold.requests[1]["messages"] if m["role"] == "tool"][0]
    assert "no such file" in tool_msg["content"]


def test_streaming_loop(app, client, kobold, accounts, upload):
    name = upload("kcstream_pub.png", seed=32)
    kobold.replies = [
        {"chunks": [{"id": "a", "choices": [{"index": 0, "delta": {"content": "Let me look. "}}]},
                    {"id": "a", "choices": [{"index": 0, "delta": {"tool_calls": [
                        {"index": 0, "id": "t1", "type": "function",
                         "function": {"name": "file_info", "arguments": ""}}]}}]},
                    {"id": "a", "choices": [{"index": 0, "delta": {"tool_calls": [
                        {"index": 0, "function": {"arguments": json.dumps({"path": name})}}]}}]},
                    {"id": "a", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}]},
        {"chunks": [{"id": "b", "choices": [{"index": 0, "delta": {"content": "It is a png."}}]},
                    {"id": "b", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                     "timings": {"predicted_n": 3}}]}]
    r = client.post("/chat/v1/chat/completions",
                    json={"stream": True, "messages": [{"role": "user", "content": "what is it"}]})
    events = [ln[6:] for ln in r.get_data(as_text=True).split("\n") if ln.startswith("data: ")]
    assert events[-1] == "[DONE]"
    objs = [json.loads(e) for e in events[:-1]]
    text = "".join(o["choices"][0]["delta"].get("content") or "" for o in objs)
    assert text.startswith("Let me look. ") and text.endswith("It is a png.")
    assert any("[tool] file_info" in (o["choices"][0]["delta"].get("reasoning_content") or "") for o in objs)
    assert not any(o["choices"][0]["delta"].get("tool_calls") for o in objs)
    finals = [o for o in objs if o["choices"][0].get("finish_reason")]
    assert len(finals) == 1 and finals[0]["choices"][0]["finish_reason"] == "stop"
    assert finals[0]["timings"]["predicted_n"] == 3
    tool_msg = [m for m in kobold.requests[1]["messages"] if m["role"] == "tool"][0]
    assert json.loads(tool_msg["content"])["path"] == name


def test_foreign_tool_calls_go_back_to_the_ui(client, kobold):
    kobold.replies = [{"tool_calls": [tool_call("ui_calculator", {"x": 1})]}]
    j = client.post("/chat/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "calc"}],
        "tools": [{"type": "function", "function": {"name": "ui_calculator", "parameters": {}}}]}).get_json()
    assert j["choices"][0]["finish_reason"] == "tool_calls"
    assert j["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "ui_calculator"
    sent = {t["function"]["name"] for t in kobold.requests[0]["tools"]}
    assert "ui_calculator" in sent and "search_library" in sent


def test_edits_wait_for_approval(app, client, kobold, accounts, upload):
    alice, bob = accounts["kc_alice"], accounts["kc_bob"]
    perms = {"data.upload": "write", "annot.tags": "write", "chat.edit": "write"}
    with as_(app, alice, **perms):
        rel = upload("kcedit.png", seed=33)
        kobold.replies = [{"tool_calls": [tool_call("propose_tags", {"paths": [rel], "add": ["kcsunset"]})]},
                          {"content": "proposed"}]
        client.post("/chat/v1/chat/completions", json={"messages": [{"role": "user", "content": "tag"}]})
        out = json.loads([m for m in kobold.requests[1]["messages"] if m["role"] == "tool"][0]["content"])
        assert out["status"] == "pending"
        aid = out["action_id"]
        assert "kcsunset" not in json.dumps(client.get(f"/api/meta/{rel}").get_json() or {})
        pending = client.get("/api/kobold_chat/actions?status=pending").get_json()["actions"]
        assert [a["id"] for a in pending] == [aid]
    # someone else cannot see or approve it
    with as_(app, bob, **perms):
        assert client.get("/api/kobold_chat/actions").get_json()["actions"] == []
        assert client.post(f"/api/kobold_chat/actions/{aid}/approve").status_code == 404
    with as_(app, alice, **perms):
        j = client.post(f"/api/kobold_chat/actions/{aid}/approve").get_json()
        assert j["success"] and j["action"]["status"] == "done", j
        row = app.module_host.db().execute("SELECT tags FROM files WHERE rel_path=?", (rel,)).fetchone()
        assert "kcsunset" in (row["tags"] or "")
        # approving twice does nothing more
        assert client.post(f"/api/kobold_chat/actions/{aid}/approve").get_json()["action"]["status"] == "done"


def test_approval_rechecks_permissions(app, client, kobold, accounts, upload):
    alice = accounts["kc_alice"]
    perms = {"data.upload": "write", "annot.description": "write", "chat.edit": "write"}
    with as_(app, alice, **perms):
        rel = upload("kcdesc.png", seed=34)
        kobold.replies = [{"tool_calls": [tool_call("propose_description",
                                                    {"path": rel, "description": "kc new text"})]},
                          {"content": "ok"}]
        client.post("/chat/v1/chat/completions", json={"messages": [{"role": "user", "content": "d"}]})
        aid = client.get("/api/kobold_chat/actions?status=pending").get_json()["actions"][0]["id"]
    # the permission was taken away before approval: nothing is written
    with as_(app, alice, **{**perms, "annot.description": "read"}):
        j = client.post(f"/api/kobold_chat/actions/{aid}/approve").get_json()
        assert j["success"] is False and j["action"]["status"] == "failed"
    row = app.module_host.db().execute("SELECT description FROM files WHERE rel_path=?", (rel,)).fetchone()
    assert "kc new text" not in (row["description"] or "")


def test_layout_and_locations(app, client, kobold, accounts):
    tools = app.module_host.get_service("kobold_chat")["tools"]
    with as_(app, accounts["kc_alice"], **{"data.upload": "write"}):
        with app.app.test_request_context("/"):
            app.app.preprocess_request()
            lay = tools.call("library_layout", {})
            sug = tools.call("suggest_location", {"filename": "song.flac", "artist": "AC/DC",
                                                  "album": "Back", "track": 3, "title": "Hells: Bells"})
            ep = tools.call("suggest_location", {"filename": "x.mkv", "kind": "video", "show": "Show",
                                                 "season": 1, "episode": 2, "personal": False})
            scope = tools.upload_scope("users/kc_alice/music")
    assert not lay["error"] and "sections" in json.loads(lay["text"])
    s = json.loads(sug["text"])["suggested"]
    assert s.endswith("/AC DC/Back/03 - Hells Bells.flac") and s.startswith("users/kc_alice/")
    assert json.loads(ep["text"])["suggested"] == "tv/Show/Season 01/Show - s01e02.mkv"
    assert scope == ("music", "personal")


def _png(seed):
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (seed * 7 % 256, 90, 160)).save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _chat_with(parts):
    return {"messages": [{"role": "user", "content": parts}]}


def test_attachments_are_saved_as_uploads(app, client, kobold, accounts):
    alice = accounts["kc_alice"]
    parts = [{"type": "text", "text": "\n\n--- File: kc.srt ---\n1\n00:00:01,000 --> 00:00:02,000\nhello"},
             {"type": "image_url", "image_url": {"url": "data:image/png;base64," + _png(41)}},
             {"type": "text", "text": "\n\n--- File: kcnotes.txt ---\nhello library"},
             {"type": "text", "text": "file these"}]
    srt = "users/kc_alice/movies/Kc Film (2020)/Kc Film (2020).srt"
    kobold.replies = [{"tool_calls": [
        tool_call("save_attachment", {"attachment": "a2", "path": "users/kc_alice/kcpics/kccat"}),
        tool_call("save_attachment", {"attachment": "a1", "path": srt}, "c2"),
        tool_call("save_attachment", {"attachment": "a3", "path": "users/kc_alice/kcnotes.txt"}, "c3")]},
        {"content": "saved"}]
    with as_(app, alice, **{"data.upload": "write"}):
        r = client.post("/chat/v1/chat/completions", json=_chat_with(parts))
        assert r.status_code == 200, r.get_data(as_text=True)
    note = kobold.requests[0]["messages"][0]["content"]
    assert "a2 image/png" in note and "a3 text/plain kcnotes.txt" in note
    raw = [m["content"] for m in kobold.requests[1]["messages"] if m["role"] == "tool"]
    # the core's upload rules apply unchanged: a loose .txt is not a library file
    assert "Unsupported file type" in raw[2]
    img, txt = [json.loads(r) for r in raw[:2]]
    assert img["stored"].startswith("users/kc_alice/kcpics/kccat"), img
    db = app.module_host.db()
    assert db.execute("SELECT 1 FROM files WHERE rel_path=?", (img["stored"],)).fetchone()
    assert txt["stored"] == srt, txt
    with open(os.path.join(app.module_host.media_dir, srt), encoding="utf-8") as fh:
        assert fh.read().endswith("hello")


def test_attachments_respect_permissions(app, client, kobold, accounts):
    bob = accounts["kc_bob"]
    parts = [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + _png(42)}}]
    # into someone else's folder: refused
    kobold.replies = [{"tool_calls": [tool_call("save_attachment",
                                                {"attachment": "a1", "path": "users/kc_alice/kcbob.png"})]},
                      {"content": "no"}]
    with as_(app, bob, **{"data.upload": "write"}):
        client.post("/chat/v1/chat/completions", json=_chat_with(parts))
    out = [m for m in kobold.requests[1]["messages"] if m["role"] == "tool"][0]["content"]
    assert "no write access" in out
    assert not app.module_host.db().execute(
        "SELECT 1 FROM files WHERE rel_path LIKE 'users/kc_alice/kcbob%'").fetchone()
    # a viewer is not offered the tool, nor told about the attachment
    kobold.requests.clear()
    kobold.replies = [{"content": "hi"}]
    with as_(app, bob, role="viewer"):
        client.post("/chat/v1/chat/completions", json=_chat_with(parts))
    names = {t["function"]["name"] for t in kobold.requests[0]["tools"]}
    assert "save_attachment" not in names and "list_attachments" not in names
    assert "save_attachment" not in kobold.requests[0]["messages"][0]["content"]


def test_paths_are_confined():
    for bad in ("../etc/passwd", "a/../../b", ".cim/x", "a/.profiles/1.jpg", ""):
        with pytest.raises(ToolError):
            clean_rel(bad)
    assert clean_rel("\\a\\b/") == "a/b"


def test_proxy_refuses_dot_segments():
    p = Proxy("lcpp", "/chat", lambda: "http://x", lambda: False, lambda pre: "")
    assert p.refused("a/../api/extra/shutdown") and p.refused("..") and p.refused("a%2f..")
    assert p.refused("Api/Extra/Shutdown")
    assert p.refused("v1/chat/completions") is None


def test_down_page_when_stopped(client, kobold, host):
    host.get_service("kobold")["running"] = lambda: False
    r = client.get("/chat/")
    assert r.status_code == 503 and b"not running" in r.get_data()