"""! @file
@brief The tool loop behind the chat UIs' /v1/chat/completions.
======================================================================
The UI posts an ordinary OpenAI chat request. The loop adds the library tools
the user may call to its `tools` (standard function tools, rendered by the
model's jinja chat template in koboldcpp), sends it on, and whenever the model
answers with tool calls for those tools it runs them here, as the user,
appends the results and asks again - until the model answers in text or the
round limit is reached. The UI only sees the final answer; with `stream` on,
text deltas are passed through as they arrive and each tool run shows as a
short reasoning line.

Tools the UI itself declared (its own frontend tools) stay the UI's: when the
model calls one of those, the round's calls for them are handed back to the UI
as normal tool_calls and the loop ends.
"""
import json
import mimetypes
import re
import time

from flask import g, has_request_context

## @brief Most tool calls run in one round.
MAX_CALLS_PER_ROUND = 8
## @brief A text file the llama.cpp UI inlined: its own text part, "--- File: name ---" first.
_TEXT_FILE = re.compile(r"^\s*--- File: (.+?) ---\n(.*)$", re.S)
_DATA_URL = re.compile(r"^data:([\w.+/-]+)(?:;[^,]*)?;base64,(.*)$", re.S)
## @brief Extensions for attachment types mimetypes guesses badly.
_EXTS = {"image/jpeg": ".jpg", "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/wav": ".wav",
         "audio/x-wav": ".wav", "audio/ogg": ".ogg", "audio/flac": ".flac", "video/ogg": ".ogv",
         "video/mp4": ".mp4", "video/webm": ".webm", "image/webp": ".webp", "image/heic": ".heic",
         "image/avif": ".avif", "text/plain": ".txt"}

SYSTEM_NOTE = (
    "You are the assistant of a self-hosted media library (photos, videos, music, movies, TV, books). "
    "The signed-in user is {user}. Use the provided tools to search and inspect the library; they only "
    "ever show what this user is allowed to see, so never guess paths. Cite library paths in answers. "
    "Before organising or uploading, call library_layout. Tools named propose_* only queue a change: "
    "nothing happens until the user approves it in the approval panel, so say so and do not claim the "
    "change is done.")


def tool_message_content(result):
    """! @brief A tool result as the `content` of a role=tool message.
    Images go in koboldcpp's tool-content JSON ({"content": [{"type": "image", ...}]}),
    which it hands to the vision projector; plain results are the text itself."""
    if not result["images"]:
        return result["text"]
    parts = [{"type": "text", "text": result["text"]}]
    parts += [{"type": "image", "data": b64, "mimeType": mime} for b64, mime in result["images"]]
    return json.dumps({"content": parts, "isError": result["error"]})


def _args(raw):
    """! @brief Tool-call arguments as a dict (they arrive as a JSON string or a dict); None when unreadable."""
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return None
    return v if isinstance(v, dict) else None


def _short(args):
    """! @brief Arguments shortened for a progress line."""
    s = json.dumps(args, ensure_ascii=False)
    return s if len(s) <= 160 else s[:157] + "..."


def _ext(mime):
    return _EXTS.get(mime) or mimetypes.guess_extension(mime) or ""


def collect_attachments(messages):
    """! @brief The files the user attached, from the messages the UI sent.
    Images arrive as image_url data URLs, audio as input_audio, video as input_video
    (base64), text files as a text part the llama.cpp UI heads "--- File: name ---".
    The data stays base64 until save_attachment decodes it.
    @return [{"id": "a1", "mime", "ext", "name", "bytes", "message", "data"}]"""
    out = []

    def add(mime, data, name, msg_no, size=None):
        out.append({"id": f"a{len(out) + 1}", "mime": mime, "ext": _ext(mime), "name": name,
                    "bytes": size if size is not None else len(data) * 3 // 4,
                    "message": msg_no, "data": data})

    for no, m in enumerate(messages or [], 1):
        if not isinstance(m, dict) or m.get("role") != "user" or not isinstance(m.get("content"), list):
            continue
        for part in m["content"]:
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if kind == "image_url":
                iu = part.get("image_url")
                url = iu.get("url") if isinstance(iu, dict) else iu
                hit = _DATA_URL.match(str(url or ""))
                if hit:
                    add(hit.group(1).lower(), hit.group(2), "", no)
            elif kind in ("input_audio", "input_video"):
                media = part.get(kind) or {}
                fmt = str(media.get("format") or "").lower()
                mime = f"{kind[6:]}/{'mpeg' if fmt == 'mp3' else fmt or 'octet-stream'}"
                if media.get("data"):
                    add(mime, media["data"], "", no)
            elif kind == "text":
                hit = _TEXT_FILE.match(part.get("text") or "")
                if hit:
                    body = hit.group(2).encode("utf-8")
                    name = hit.group(1).strip()
                    mime = mimetypes.guess_type(name)[0] or "text/plain"
                    add(mime, body, name, no, len(body))
    return out


def attachments_note(atts):
    """! @brief The system-note line naming the attachments for the model."""
    if not atts:
        return ""
    items = "; ".join(f"{a['id']} {a['mime']}{' ' + a['name'] if a['name'] else ''} "
                      f"({a['bytes'] // 1024} KB, message {a['message']})" for a in atts)
    return ("Files the user attached in this conversation (store one with save_attachment, "
            "after suggest_location): " + items)


class Agent:
    """! @brief One chat completion request, run with the library tools.
    @param tools     the ChatTools instance (permission-aware, request-bound)
    @param post      post(body, stream) -> requests.Response to the model server
    @param rounds    most model calls per request
    @param extra     extra system instructions (a setting)
    """

    def __init__(self, tools, post, rounds=8, extra=""):
        """! @brief Keep the tools, the upstream call and the limits."""
        self.tools = tools
        self.post = post
        self.rounds = max(1, int(rounds or 8))
        self.extra = str(extra or "").strip()

    # -- request shaping -------------------------------------------------------
    def prepare(self, body):
        """! @brief The request with our tools merged in and the system note added.
        @return (body, our tool names); names empty when the user gets no tools."""
        defs = self.tools.definitions()
        ours = {d["function"]["name"] for d in defs}
        if not ours:
            return body, set()
        body = dict(body)
        theirs = [t for t in (body.get("tools") or [])
                  if isinstance(t, dict) and (t.get("function") or {}).get("name") not in ours]
        body["tools"] = defs + theirs
        if body.get("tool_choice") in (None, "none"):
            body["tool_choice"] = "auto"
        note = SYSTEM_NOTE.format(user=self.tools.username() or "unknown")
        atts = collect_attachments(body.get("messages"))
        if has_request_context():
            g.kobold_chat_attachments = atts
        if atts and "save_attachment" in ours:
            note += "\n" + attachments_note(atts)
        if self.extra:
            note += "\n" + self.extra
        msgs = list(body.get("messages") or [])
        if msgs and msgs[0].get("role") == "system":
            first = dict(msgs[0])
            c = first.get("content")
            if isinstance(c, list):
                first["content"] = c + [{"type": "text", "text": "\n\n" + note}]
            else:
                first["content"] = (c or "") + "\n\n" + note
            msgs[0] = first
        else:
            msgs.insert(0, {"role": "system", "content": note})
        body["messages"] = msgs
        return body, ours

    def _run_calls(self, calls, messages, content):
        """! @brief Run our tool calls and append the assistant turn + results to messages.
        @return [(name, args, ok)] for the progress lines."""
        messages.append({"role": "assistant", "content": content or "",
                         "tool_calls": [{"id": c["id"], "type": "function",
                                         "function": {"name": c["name"], "arguments": c["arguments"]
                                                      if isinstance(c["arguments"], str)
                                                      else json.dumps(c["arguments"])}}
                                        for c in calls]})
        done = []
        for c in calls[:MAX_CALLS_PER_ROUND]:
            args = _args(c["arguments"])
            if args is None:
                res = {"text": "arguments were not valid JSON", "images": [], "error": True}
            else:
                res = self.tools.call(c["name"], args)
            messages.append({"role": "tool", "tool_call_id": c["id"], "name": c["name"],
                             "content": tool_message_content(res)})
            done.append((c["name"], args or {}, not res["error"]))
        for c in calls[MAX_CALLS_PER_ROUND:]:
            messages.append({"role": "tool", "tool_call_id": c["id"], "name": c["name"],
                             "content": f"skipped: at most {MAX_CALLS_PER_ROUND} tool calls per turn"})
        return done

    @staticmethod
    def _calls_of(message):
        """! @brief The tool calls of a non-streamed assistant message."""
        out = []
        for i, tc in enumerate(message.get("tool_calls") or []):
            fn = tc.get("function") or {}
            if fn.get("name"):
                out.append({"id": tc.get("id") or f"call_{i}", "name": fn["name"],
                            "arguments": fn.get("arguments") or "{}", "raw": tc})
        return out

    # -- non-streaming -----------------------------------------------------------
    def complete(self, body):
        """! @brief Run the loop without streaming. @return (json dict, status)."""
        body, ours = self.prepare(body)
        body["stream"] = False
        msgs = body["messages"] = list(body["messages"])
        trace = []
        for rnd in range(self.rounds):
            if rnd == self.rounds - 1 and ours:
                body["tool_choice"] = "none"
            r = self.post(body, False)
            if r.status_code != 200:
                return _json_or_error(r), r.status_code
            data = r.json()
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            calls = self._calls_of(message)
            foreign = [c for c in calls if c["name"] not in ours]
            if not calls or foreign:
                if foreign:
                    message["tool_calls"] = [c["raw"] for c in foreign]
                    choice["finish_reason"] = "tool_calls"
                elif "tool_calls" in message:
                    message.pop("tool_calls")
                if trace:
                    message["reasoning_content"] = "\n".join(trace) + (
                        "\n" + message["reasoning_content"] if message.get("reasoning_content") else "")
                return data, 200
            for name, args, ok in self._run_calls(calls, msgs, message.get("content")):
                trace.append(_progress(name, args, ok))
        return {"error": {"message": "tool loop ended without an answer"}}, 500

    # -- streaming -------------------------------------------------------------------
    def stream(self, body):
        """! @brief Run the loop, yielding SSE bytes for the UI."""
        body, ours = self.prepare(body)
        body["stream"] = True
        msgs = body["messages"] = list(body["messages"])
        meta = {"id": f"chatcmpl-cim{int(time.time() * 1000)}", "model": body.get("model") or "koboldcpp"}

        def chunk(delta, finish=None, extra=None):
            """! @brief One SSE chunk of our own in the stream's id / model."""
            obj = {"id": meta["id"], "object": "chat.completion.chunk", "created": int(time.time()),
                   "model": meta["model"], "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            if extra:
                obj.update(extra)
            return f"data: {json.dumps(obj)}\n\n".encode()

        for rnd in range(self.rounds):
            if rnd == self.rounds - 1 and ours:
                body["tool_choice"] = "none"
            r = self.post(body, True)
            try:
                if r.status_code != 200 or "text/event-stream" not in (r.headers.get("content-type") or ""):
                    data = _json_or_error(r)
                    if r.status_code == 200 and data.get("choices"):
                        message = data["choices"][0].get("message") or {}
                        calls = self._calls_of(message)
                        if calls and not [c for c in calls if c["name"] not in ours]:
                            for name, args, ok in self._run_calls(calls, msgs, message.get("content")):
                                yield chunk({"reasoning_content": _progress(name, args, ok) + "\n"})
                            continue
                        yield chunk({"content": message.get("content") or ""}, "stop")
                    else:
                        msg = (data.get("error") or {}).get("message") if isinstance(data.get("error"), dict) \
                            else data.get("error") or f"model server answered {r.status_code}"
                        yield chunk({"content": f"[chat error: {msg}]"}, "stop")
                    yield b"data: [DONE]\n\n"
                    return
                acc, content, finish, tail = {}, [], None, []
                for obj in _sse(r):
                    if obj is None:
                        break
                    meta["id"] = obj.get("id") or meta["id"]
                    meta["model"] = obj.get("model") or meta["model"]
                    choices = obj.get("choices") or []
                    if not choices:
                        tail.append(obj)  # a usage / timings chunk: keep it for the very end
                        continue
                    ch = choices[0]
                    delta = dict(ch.get("delta") or {})
                    for tc in delta.pop("tool_calls", None) or []:
                        slot = acc.setdefault(tc.get("index", len(acc)), {"id": "", "name": "", "arguments": ""})
                        slot["id"] = tc.get("id") or slot["id"]
                        fn = tc.get("function") or {}
                        slot["name"] = fn.get("name") or slot["name"]
                        a = fn.get("arguments")
                        if isinstance(a, dict):
                            slot["arguments"] = json.dumps(a)
                        elif a:
                            slot["arguments"] += a
                    if ch.get("finish_reason"):
                        # sent (as the real finish) only once the loop is over
                        finish = {**obj, "choices": [{**ch, "delta": {
                            k: v for k, v in delta.items() if k not in ("content", "reasoning_content")}}]}
                    if delta.get("content"):
                        content.append(delta["content"])
                    if any(delta.get(k) for k in ("content", "reasoning_content")):
                        out = {**obj, "choices": [{**ch, "delta": delta, "finish_reason": None}]}
                        yield f"data: {json.dumps(out)}\n\n".encode()
            finally:
                r.close()
            calls = [{"id": v["id"] or f"call_{i}", "name": v["name"], "arguments": v["arguments"] or "{}"}
                     for i, v in sorted(acc.items()) if v["name"]]
            foreign = [c for c in calls if c["name"] not in ours]
            if calls and not foreign:
                if content:
                    yield chunk({"content": "\n\n"})
                for name, args, ok in self._run_calls(calls, msgs, "".join(content)):
                    yield chunk({"reasoning_content": _progress(name, args, ok) + "\n"})
                continue
            if foreign:
                for i, c in enumerate(foreign):
                    yield chunk({"tool_calls": [{"index": i, "id": c["id"], "type": "function",
                                                 "function": {"name": c["name"], "arguments": c["arguments"]}}]})
                yield chunk({}, "tool_calls")
            elif finish is not None:
                ch = finish["choices"][0]
                if ch.get("finish_reason") == "tool_calls":
                    ch["finish_reason"] = "stop"
                yield f"data: {json.dumps(finish)}\n\n".encode()
            else:
                yield chunk({}, "stop")
            for obj in tail:
                yield f"data: {json.dumps(obj)}\n\n".encode()
            yield b"data: [DONE]\n\n"
            return
        yield chunk({"content": "\n[stopped: too many tool rounds]"}, "stop")
        yield b"data: [DONE]\n\n"


def _progress(name, args, ok):
    """! @brief One progress line for a tool run."""
    return f"[tool] {name}({_short(args)})" + ("" if ok else " - failed")


def _json_or_error(r):
    """! @brief A reply's JSON, or an OpenAI-style error object."""
    try:
        data = r.json()
        return data if isinstance(data, dict) else {"error": {"message": str(data)[:300]}}
    except ValueError:
        return {"error": {"message": (r.text or "")[:300] or f"HTTP {r.status_code}"}}


def _sse(r):
    """! @brief The JSON objects of an OpenAI SSE stream; None at [DONE]."""
    buf = []
    for line in r.iter_lines(decode_unicode=False):
        if line is None:
            continue
        line = line.decode("utf-8", "replace") if isinstance(line, bytes) else line
        if not line.strip():
            if buf:
                data, buf = "\n".join(buf), []
                if data.strip() == "[DONE]":
                    yield None
                    return
                try:
                    yield json.loads(data)
                except ValueError:
                    continue
            continue
        if line.startswith("data:"):
            buf.append(line[5:].lstrip())
    if buf:
        data = "\n".join(buf)
        if data.strip() != "[DONE]":
            try:
                yield json.loads(data)
            except ValueError:
                pass