"""! @file
@brief vlm book tools: AI tag / blurb routes behind the buttons vlm registers in the
books module's editor hook, answered from the `books` service's content (no endpoint needed: the chat call is
faked)."""
import pytest

from modules.books.tests.test_book_uploads import _book, _cbz, _epub, _up, prefs  # noqa: F401
from modules.vlm import book_tools as bt

_PARA = "<p>" + "Words of a real chapter go here. " * 20 + "</p>"


@pytest.mark.parametrize("raw,want", [
    ('["Science Fiction", "space opera", "science fiction"]', ["science fiction", "space opera"]),
    ('Sure! ```json\n["mystery", "victorian london"]\n```', ["mystery", "victorian london"]),
    ("fantasy, coming of age\n- dragons", ["fantasy", "coming of age", "dragons"]),
    (["Horror", {"tag": "gothic"}, "novel"], ["horror", "gothic"]),
    ("", []),
])
def test_parse_tags(raw, want):
    assert bt.parse_tags(raw) == want


def test_no_core_ui_declared_for_books(app):
    """The book buttons go in through the books module's own hook (static/book_tools.js),
    not as core-declared buttons."""
    assert not [b for b in app.module_host.ui["buttons"]
                if b["module_id"] == "vlm" and "book" in b["area"]]


@pytest.fixture
def fake_chat(monkeypatch, app):
    seen = {}

    def call(prompt, image, want="text", **k):
        seen["prompt"], seen["image"] = prompt, image
        return seen.get("reply", '["space opera", "first contact"]')
    monkeypatch.setattr(bt.client, "call", call)
    monkeypatch.setitem(app.module_host.config, "oai_endpoint", "http://localhost:1/v1")
    monkeypatch.setitem(app.module_host.config, "oai_model", "m")
    return seen


def _run(client, what, rel):
    return client.post(f"/api/vlm/book/{what}", json={"rel_path": rel}).get_json()


def test_book_tags_answer_for_the_editor(client, upload, prefs, fake_chat, ungated):
    prefs(target=".epub", mode="none")
    rel = _up(upload, _epub([f'<html><body><h1>One</h1>{_PARA}</body></html>']), "vlm_tagme.epub")
    assert _book(client, rel)
    client.post("/api/books/meta", json={"rel_path": rel, "tags": ["space opera"]})
    j = _run(client, "tags", rel)
    assert j["success"], j
    assert j["tags"] == ["space opera", "first contact"]
    assert "first contact" in j["message"] and "space opera" not in j["message"]
    assert "Words of a real chapter" in fake_chat["prompt"]
    assert "Reuse these existing tags where they fit: space opera" in fake_chat["prompt"]
    # nothing written: the book editor applies and saves the answer
    d = client.get(f"/api/books/detail?rel_path={rel}").get_json()
    assert d["book"]["tags"] == ["space opera"]


def test_book_blurb_of_a_comic_uses_its_first_page(client, upload, prefs, fake_chat, ungated):
    prefs(target=".epub", mode="none")
    rel = _up(upload, _cbz(), "vlm_blurb.cbz")
    assert _book(client, rel)
    fake_chat["reply"] = "A caped hero saves the city."
    j = _run(client, "describe", rel)
    assert j["success"] and j["description"] == "A caped hero saves the city.", j
    assert fake_chat["image"] is not None and fake_chat["image"].shape[:2] == (60, 40)


def test_book_tools_need_a_book(client, fake_chat, ungated):
    j = _run(client, "tags", "not/a/book.epub")
    assert not j["success"] and "Open a book" in j["error"]