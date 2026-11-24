"""! @file
@brief AI tools for books: tags and a jacket blurb from the configured chat model.

The books module only reads books: it offers tool slots in its editor (the
front-end hook registerBookTool) and the `books` service's content(rel) /
tag_vocabulary(). This file adds two routes, and static/book_tools.js registers
a button for each in the book editor when it finds the hook. Each route answers
with `tags` / `description`; the book editor merges them and its autosave
writes them, like the image editor's AI buttons. Nothing here writes to a book,
and with the books module off nothing registers and the routes say so.

A text book is sent as sampled TEXT (the opening and a spread of passages - a
400k-word novel is not going in a context window). A paged book with no text (a
comic, a scanned PDF) is sent its first page as the image, with any OCR
transcript the comic tools stored.
"""
import json
import re

from flask import jsonify, request

from . import client

## @brief Default prompts; each is an editable setting (vlm_prompt_<key>).
PROMPTS = {
    "book_tag": "Give 6-15 short lowercase tags for this book: genre and subgenre, main themes, "
                "setting (time / place), tone, and audience (e.g. 'young adult'). No author names, "
                "no title words, no 'book' / 'novel' / 'fiction' on its own. "
                "Respond ONLY as a JSON list of strings.",
    "book_describe": "Write a 3-5 sentence jacket blurb: what it is about, its tone, and who would "
                     "enjoy it. No spoilers past the opening act. Reply with the blurb only - no "
                     "preamble, no headings.",
}

## @brief Words that say nothing as a tag of a book.
_NOISE = {"book", "novel", "fiction", "tags", "tag", "ebook"}


def parse_tags(raw) -> list[str]:
    """! @brief Tags out of a chat reply: a JSON array (possibly fenced or inside
    prose), else comma / newline separated. Lowercased, de-duplicated, trimmed."""
    if isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        txt = str(raw or "")
        items = None
        m = re.search(r"\[.*\]", txt, re.S)
        if m:
            try:
                v = json.loads(m.group(0))
                if isinstance(v, list):
                    items = v
            except ValueError:
                pass
        if items is None:
            items = re.split(r"[,\n;]", re.sub(r"```\w*", "", txt))
    out, seen = [], set()
    for t in items:
        if isinstance(t, dict):
            t = t.get("tag") or t.get("name") or ""
        t = re.sub(r"^[\s\-*\d.)#\"']+|[\s\"'.]+$", "", str(t)).strip().lower()
        t = re.sub(r"\s+", " ", t)
        if t and len(t) <= 40 and t not in seen and t not in _NOISE:
            seen.add(t)
            out.append(t)
    return out[:20]


def _intro(c) -> str:
    """! @brief The line that says which book this is."""
    who = ", ".join(c.get("authors") or []) or "an unknown author"
    s = f"This is a book titled {c.get('title') or c['rel_path']!r} by {who}"
    if c.get("series"):
        s += f", series {c['series']!r}"
    return s + ".\n"


def _ask(c, question: str) -> str:
    """! @brief One chat call about a book: its text, or its first page as the image."""
    if c.get("image") is not None:
        body = _intro(c) + "The image is its cover / first page.\n"
        if c.get("text"):
            body += f"Text read from its pages:\n{c['text']}\n"
        return client.call(body + "\n" + question, c["image"], "text") or ""
    return client.call(_intro(c) + question + "\n\nExcerpts:\n\n" + c["text"], None, "text") or ""


def register(host, configured):
    """! @brief Declare the book buttons and their prompts.
    @param configured  fn() -> True when an endpoint and a chat model are set."""
    for key, text in PROMPTS.items():
        host.add_config_key(f"vlm_prompt_{key}", default=text,
                            validate=lambda v: str(v) if v is not None else "")
    host.add_settings_field(key="vlm_prompt_book_tag", label="Book tags prompt", kind="textarea",
                            pane="module", help="The book's text (or first page) and its existing "
                                                "shelf tags are added to it.")
    host.add_settings_field(key="vlm_prompt_book_describe", label="Book blurb prompt",
                            kind="textarea", pane="module")

    def _prompt(key):
        return (host.config.get(f"vlm_prompt_{key}") or "").strip() or PROMPTS[key]

    def _content(body):
        if not configured():
            raise RuntimeError("Set the OAI endpoint and chat model first (Settings > Modules > Vision LLM).")
        books = host.get_service("books")
        if not books or not books.get("content"):
            raise RuntimeError("The Books module is off.")
        c = books["content"](body.get("rel_path") or "")
        if not c:
            raise RuntimeError("Open a book first.")
        if not c.get("text") and c.get("image") is None:
            raise RuntimeError("No text extracted from this book yet - run Extract first.")
        return books, c

    def _tags(body):
        books, c = _content(body)
        vocab = (books.get("tag_vocabulary") or (lambda **k: []))(limit=80)
        q = _prompt("book_tag")
        if vocab:
            q += " Reuse these existing tags where they fit: " + ", ".join(vocab) + "."
        tags = parse_tags(_ask(c, q))
        if not tags:
            return {"success": False, "error": "no tags in the response"}
        have = {str(t).lower() for t in c.get("tags") or []}
        new = [t for t in tags if t not in have]
        return {"tags": tags,
                "message": f"Added {len(new)} tag(s): {', '.join(new)}" if new else "No new tags suggested."}

    def _describe(body):
        _books, c = _content(body)
        text = _ask(c, _prompt("book_describe")).strip()
        if not text:
            return {"success": False, "error": "empty response"}
        return {"description": text}

    def _route(fn):
        def view():
            try:
                out = fn(request.get_json(silent=True) or {})
            except RuntimeError as e:
                return jsonify({"success": False, "error": str(e)})
            except Exception as e:  # the endpoint failed: say so, don't 500
                host.logger.warning(f"vlm book tool: {e}")
                return jsonify({"success": False, "error": str(e) or type(e).__name__})
            out.setdefault("success", True)
            return jsonify(out)
        view.__name__ = "vlm_" + fn.__name__.strip("_")
        return view

    host.add_route("/api/vlm/book/tags", _route(_tags), methods=["POST"],
                   feature="ai.llm", level="write")
    host.add_route("/api/vlm/book/describe", _route(_describe), methods=["POST"],
                   feature="ai.llm", level="write")
