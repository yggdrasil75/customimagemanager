"""! @file
@brief Library chat: koboldcpp's chat pages behind the app's login, with library tools.
======================================================================
While the kobold module's server is running:

  /chat/    the llama.cpp web UI koboldcpp ships (its /lcpp/)
  /chatk/   KoboldAI Lite (koboldcpp's own page; use an instruct / chat mode
            so it sends chat completions, which is where the tools live)

Both are proxied (proxy.py): the app's login gate covers them, the browser's
cookies never reach koboldcpp, and endpoints with server-wide effects are
refused. Each chat request goes through the tool loop (agent.py), which offers
the model standard function tools (rendered by the model's jinja template,
koboldcpp --jinja) and runs the calls here, as the signed-in user (tools.py):
search, look at pictures, read books and documents, list folders and albums,
learn where files belong, and store files the user attached in the chat
(save_attachment: an ordinary upload, with the user's upload permission and quota).

Edits (tags, descriptions, moves, albums) are only proposals: each one waits in
the approval panel the page shows, and is applied, with every permission check
run again, only when the same user approves it.

Permissions: `chat.use` (read) opens the pages and the read tools; viewers have
it, uploaders do not, and accounts with the uploader role are refused outright.
`chat.edit` (write) plus the gallery feature of the edit (annot.tags,
annot.description, data.move, tab.albums) at write offers a proposal tool;
viewers get none. Nothing a tool returns goes beyond what the user's gallery
would show.

Routes: /chat/, /chatk/ (pages and their API), /chat/_cim/session.js,
/api/kobold_chat/actions (GET), /api/kobold_chat/actions/<id>/approve|deny (POST).
"""
import json

import requests
from flask import Response, jsonify, redirect, request, stream_with_context, g

from . import agent as agent_mod
from . import proxy as proxy_mod
from . import tools as tools_mod

MANIFEST = {
    "id":          "kobold_chat",
    "name":        "Library chat (KoboldCpp)",
    "version":     "1.0.0",
    "description": "Chat with the library at /chat (llama.cpp UI) and /chatk (KoboldAI Lite): the "
                   "model searches, views and reads the user's files with tools; edits need approval.",
    "core":        False,
    "requires":    ["kobold"],
    "pip":         [],
}

SHIM = "/modules/kobold_chat/static/chat_shim.js"
DOWN_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Chat unavailable</title>
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="font-family:system-ui,sans-serif;background:#111827;color:#e5e7eb;padding:3rem">
<h1 style="font-size:1.4rem">The chat server is not running</h1>
<p>{reason}</p><p><a style="color:#60a5fa" href="/">Back to the library</a></p></body></html>"""


def register(host):
    """! @brief Mount /chat and /chatk, the actions API and the chat features."""
    kobold = host.get_service("kobold")
    if not kobold:
        host.logger.warning("kobold_chat: kobold service missing; /chat not registered")
        return
    tools = tools_mod.ChatTools(host)

    host.register_feature(tools_mod.USE_FEATURE, "Library chat (/chat, /chatk) and its read tools",
                          section="chat", section_label="Library chat", default="read",
                          role_defaults={"viewer": "read", "uploader": "block"})
    host.register_feature(tools_mod.EDIT_FEATURE, "Library chat: propose edits (applied after approval)",
                          section="chat", section_label="Library chat", default="write",
                          role_defaults={"viewer": "block", "uploader": "block"})
    host.add_table(tools_mod.ACTIONS_DDL, kind="state")

    host.add_config_key("kobold_chat_rounds", default=8,
                        validate=lambda v: max(1, min(20, int(v))))
    host.add_settings_field(key="kobold_chat_rounds", label="Tool rounds per message", kind="number",
                            pane="module", help="Most model calls one chat message may take (1-20).")
    host.add_config_key("kobold_chat_prompt", default="", validate=lambda v: str(v or "")[:4000])
    host.add_settings_field(key="kobold_chat_prompt", label="Extra instructions", kind="textarea",
                            pane="module", help="Appended to the system note the chat model gets.")

    def base_url():
        """! @brief The kobold module's server address."""
        return kobold["url"]().rstrip("/")

    def shim_tags(prefix):
        """! @brief The <script> tags injected into a proxied page."""
        return (f'<script src="{prefix}/_cim/session.js"></script>'
                f'<script src="{SHIM}"></script>')

    proxies = {"/chat": proxy_mod.Proxy("lcpp", "/chat", base_url, host.is_admin, shim_tags),
               "/chatk": proxy_mod.Proxy("lite", "/chatk", base_url, host.is_admin, shim_tags)}

    def refuse_user():
        """! @brief Why the requester may not chat at all (logged), or None."""
        why = tools.blocked()
        if why:
            host.logger.info(f"chat refused for {tools.username()!r}: {why}")
        return why

    def session_js(prefix):
        """! @brief The per-session values the shim needs (CSRF token, user, routes)."""
        u = g.get("user") or {}
        data = {"csrf": g.session["csrf"] if g.get("session") else None,
                "user": u.get("username") or "", "prefix": prefix,
                "actions": "/api/kobold_chat/actions",
                "can_edit": tools.has(tools_mod.EDIT_FEATURE, "write")}
        return Response("window.CIM_CHAT = " + json.dumps(data) + ";\n", 200,
                        {"Content-Type": "application/javascript", "Cache-Control": "no-store"})

    def completions(px):
        """! @brief A chat completion through the tool loop (streamed or not)."""
        body = request.get_json(force=True, silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": {"message": "invalid JSON body"}}), 400
        ag = agent_mod.Agent(
            tools,
            lambda b, stream: px.send("POST", "v1/chat/completions", json_body=b, stream=stream,
                                      headers={"Content-Type": "application/json",
                                               "Accept": "text/event-stream" if stream else "application/json"}),
            rounds=host.config.get("kobold_chat_rounds") or 8,
            extra=host.config.get("kobold_chat_prompt") or "")
        if body.get("stream"):
            return Response(stream_with_context(ag.stream(body)), 200,
                            {"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})
        data, status = ag.complete(body)
        return jsonify(data), status

    def lite_version(px):
        """! @brief Lite's feature probe with the refused server-wide extras switched off."""
        r = px.send("GET", "api/extra/version")
        try:
            data = r.json()
        except ValueError:
            return Response(r.content, r.status_code)
        # server-wide extras the proxy refuses: Lite should not offer them
        data.update({"savedata": False, "multiplayer": False, "websearch": False, "mcp": False,
                     "admin": 0 if not host.is_admin() else data.get("admin", 0)})
        return jsonify(data), r.status_code

    def handle(prefix, sub):
        """! @brief Dispatch one request under a mount: our routes, refusals, the tool loop, else forward."""
        px = proxies[prefix]
        why = refuse_user()
        if why:
            return jsonify({"error": {"message": why}}), 403
        if sub == "_cim/session.js":
            return session_js(prefix)
        why = px.refused(sub)
        if why:
            return jsonify({"error": {"message": why}}), 403 if why == "admin only" else 404
        try:
            if sub in ("", "index.html") and request.method == "GET":
                if not kobold["running"]():
                    return Response(DOWN_PAGE.format(
                        reason="An admin can start it in Settings -> Kobold."), 503,
                        {"Content-Type": "text/html; charset=utf-8"})
                return px.page("")
            if sub.strip("/") == "v1/chat/completions" and request.method == "POST":
                return completions(px)
            if px.ui == "lite" and sub.strip("/") == "api/extra/version":
                return lite_version(px)
            return px.forward(sub)
        except requests.RequestException as e:
            host.logger.info(f"chat proxy: koboldcpp unreachable ({e})")
            if request.method == "GET" and "html" in (request.headers.get("Accept") or ""):
                return Response(DOWN_PAGE.format(reason="The model server is starting or stopped."), 503,
                                {"Content-Type": "text/html; charset=utf-8"})
            return jsonify({"error": {"message": "the model server is not reachable"}}), 503

    methods = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]
    for prefix in proxies:
        def root_redirect(prefix=prefix):
            """! @brief /chat -> /chat/ (the pages resolve URLs against the trailing slash)."""
            return redirect(prefix + "/")

        def root(prefix=prefix):
            """! @brief The UI's page."""
            return handle(prefix, "")

        def sub_path(sub, prefix=prefix):
            """! @brief Anything below the mount."""
            return handle(prefix, sub)
        name = prefix.strip("/")
        host.add_route(prefix, root_redirect, feature=tools_mod.USE_FEATURE, endpoint=f"kobold_chat_{name}_redirect")
        host.add_route(prefix + "/", root, feature=tools_mod.USE_FEATURE, methods=methods,
                       endpoint=f"kobold_chat_{name}_root", strict_slashes=True)
        host.add_route(prefix + "/<path:sub>", sub_path, feature=tools_mod.USE_FEATURE, methods=methods,
                       endpoint=f"kobold_chat_{name}_sub")

    # -- proposals ---------------------------------------------------------
    def api_actions():
        """! @brief The requester's proposals, optionally of one status."""
        why = refuse_user()
        if why:
            return jsonify({"success": False, "error": why}), 403
        status = request.args.get("status") or ""
        db = host.db()
        if status:
            rows = db.execute("SELECT * FROM chat_actions WHERE username=? AND status=? ORDER BY id DESC "
                              "LIMIT 100", (tools.username(), status)).fetchall()
        else:
            rows = db.execute("SELECT * FROM chat_actions WHERE username=? ORDER BY id DESC LIMIT 100",
                              (tools.username(),)).fetchall()
        return jsonify({"success": True, "actions": [tools_mod.action_row(r) for r in rows]})

    def decide(action_id, approve):
        """! @brief Approve or deny one of the requester's proposals."""
        why = refuse_user()
        if why:
            return jsonify({"success": False, "error": why}), 403
        row = tools.decide(action_id, approve)
        if row is None:
            return jsonify({"success": False, "error": "no such proposal"}), 404
        ok = row["status"] in ("done", "denied")
        return jsonify({"success": ok, "action": row, "message": row["result"] or row["status"],
                        **({} if ok else {"error": row["result"]})})

    def api_approve(action_id):
        """! @brief Apply a proposal (every check runs again)."""
        return decide(action_id, True)

    def api_deny(action_id):
        """! @brief Drop a proposal."""
        return decide(action_id, False)

    host.add_route("/api/kobold_chat/actions", api_actions, feature=tools_mod.USE_FEATURE)
    host.add_route("/api/kobold_chat/actions/<int:action_id>/approve", api_approve, methods=["POST"],
                   feature=tools_mod.EDIT_FEATURE, level="write", action="chat_approve")
    host.add_route("/api/kobold_chat/actions/<int:action_id>/deny", api_deny, methods=["POST"],
                   feature=tools_mod.USE_FEATURE, action="chat_deny")
    host.provide_service("kobold_chat", {"tools": tools})
    host.logger.info("kobold_chat module: /chat (llama.cpp UI) and /chatk (KoboldAI Lite)")