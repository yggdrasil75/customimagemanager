"""! @file
@brief Webcomic module: site profiles read saved page shapes offline, archives grow
in place, and a subscription run through the fetch queue picks up new pages.
    ./run_tests.sh modules/webcomic
"""
import io
import os
import shutil
import sys
import time
import zipfile

import pytest

from cimtest import png_bytes, post_json


def _mod(name):
    for k, m in list(sys.modules.items()):
        if k.endswith("webcomic." + name):
            return m
    pytest.skip("webcomic module not loaded")


# -- profiles against page shapes ------------------------------------------------

WEBTOON = """<html><head><meta property="og:site_name" content="Strip Site"></head><body>
<div class="subj_info"><a class="subj" href="/list">Tall Tale</a></div>
<h1 class="subj_episode">Episode 1</h1>
<div id="_imageList" class="viewer_img">
 <img src="https://static.example/bg_transparency.png" data-url="https://img.example/a/001.jpg" class="_images">
 <img src="https://static.example/bg_transparency.png" data-url="https://img.example/a/002.jpg" class="_images">
</div>
<a href="https://www.webtoons.com/en/fantasy/tall-tale/episode-2/viewer?title_no=1&episode_no=2" class="pg_next _nextEpisode">Next Episode</a>
<a href="https://www.patreon.com/x">Next</a>
</body></html>"""

FULLPAGE = """<html><body><div id="nav"><a href="ch01_01.html">first</a>
<a href="ch01_02.html" class="back">back</a><a href="ch01_04.html" class="forward">forward</a></div>
<div id="comic"><img src="../../pageart/ch01_03.jpg">
 <div style="background-image:url('../../pageart/bg03.png')"><p>Text set beside the art</p></div>
 <script>track()</script></div>
<img src="/images/logo.png"></body></html>"""

STRIP = """<html><body><a href="/strip.php?s=1"><img src="first.png" alt="First"></a>
<a href="/strip.php?s=41"><img id="prevOne" src="prev.png" alt="Previous"></a>
<img src="S_0042.jpg" id="strip">
<a href="/strip.php?s=43"><img id="nextOne" src="next.png" alt="Next"></a>
<a href="/strip.php?s=1300"><img src="last.png" alt="Last"></a></body></html>"""


def test_webtoon_style():
    sc = _mod("scrape")
    url = "https://www.webtoons.com/en/fantasy/tall-tale/episode-1/viewer?title_no=1&episode_no=1"
    p = sc.profile_for(url)
    assert p["id"] == "webtoon"
    pg = sc.parse_page(WEBTOON, url, p)
    assert pg["images"] == ["https://img.example/a/001.jpg",
                            "https://img.example/a/002.jpg"]   # data-url, not the placeholder
    assert pg["next"].endswith("episode_no=2")
    assert pg["title"] == "Episode 1" and pg["series"] == "Tall Tale"
    assert sc.group_of(p, url, 0, pg) == ("ch00001", "Episode 001")


def test_full_page_style_keeps_the_page_body():
    sc = _mod("scrape")
    url = "https://comic.example/story/comic/ch01/ch01_03.html"
    p = sc.profile_for(url, "fullpage")
    assert p["output"] == "epub"
    pg = sc.parse_page(FULLPAGE, url, p)
    assert pg["images"] == ["https://comic.example/story/pageart/ch01_03.jpg"]
    assert pg["next"] == "https://comic.example/story/comic/ch01/ch01_04.html"
    assert "Text set beside the art" in pg["content_html"] and "track()" not in pg["content_html"]
    assert sc.group_of(p, url, 0, pg) == ("ch00001", "Chapter 001")


def test_generic_style_on_a_strip_and_fixed_size_files():
    sc = _mod("scrape")
    url = "https://strips.example/strip.php?s=42"
    p = sc.profile_for(url)
    assert p["id"] == "generic"
    pg = sc.parse_page(STRIP, url, p)
    assert pg["images"] == ["https://strips.example/S_0042.jpg"]
    assert pg["next"].endswith("s=43")
    p = dict(p, group="volume", volume_size=100)
    assert sc.group_of(p, url, 41) == ("v0000", "0001-0100")
    assert sc.group_of(p, url, 100) == ("v0001", "0101-0200")


def test_chapters_of_any_length_without_chapter_urls():
    """! @brief URLs say nothing about chapters (/page/57): markers on the page start a
    chapter, unmarked pages stay in the one before, whatever its length."""
    sc = _mod("scrape")
    p = sc.profile_for("https://x.example/page/1", "fullpage")
    def pg(html, u):
        return sc.parse_page(f"<html><head><title>{html}</title></head><body><div id='comic'>"
                             f"<img src='/a.png'></div></body></html>", u, p)
    prev = None
    seen = []
    for i, title in enumerate(["My Comic - Chapter 1: Start", "My Comic - page", "My Comic - page",
                               "My Comic - Ch. 2 cover", "My Comic - page", "My Comic - Chapter 10"]):
        u = f"https://x.example/page/{i + 1}"
        prev = sc.group_of(p, u, i, pg(title, u), prev=prev)
        seen.append(prev[1])
    assert seen == ["Chapter 001", "Chapter 001", "Chapter 001", "Chapter 002", "Chapter 002", "Chapter 010"]
    # a chapter drop-down / heading picked by your selector
    p2 = dict(p, chapter_sel="select#chap")
    page = sc.parse_page("<html><body><select id='chap'><option>Prologue</option>"
                         "<option selected>Chapter 7 - Rain</option></select><div id='comic'><img src='/b.png'>"
                         "</div></body></html>", "https://x.example/page/90", p2)
    assert sc.group_of(p2, "https://x.example/page/90", 89, page) == ("ch00007", "Chapter 007")
    # nothing found anywhere yet: fixed-size files until a marker turns up
    bare = sc.parse_page("<html><body><div id='comic'><img src='/c.png'></div></body></html>",
                         "https://x.example/page/3", p)
    assert sc.group_of(dict(p, volume_size=50), "https://x.example/page/3", 2, bare) == ("v0000", "0001-0050")


def test_generic_heuristics():
    sc = _mod("scrape")
    html = """<html><body><img src="/logo.png" width="900" height="100">
    <div class="post"><img src="/comics/2024-01-01.png" width="800" height="600"></div>
    <a href="https://ads.example/next">Next</a><a href="/comic/2" rel="next">›</a></body></html>"""
    pg = sc.parse_page(html, "https://my.comic/comic/1", sc.profile_for("https://my.comic/comic/1"))
    assert pg["images"] == ["https://my.comic/comics/2024-01-01.png"]
    assert pg["next"] == "https://my.comic/comic/2"


# -- writers ----------------------------------------------------------------------

def test_cbz_grows_in_place(tmp_path):
    w = _mod("writers").CbzWriter(str(tmp_path), "Strip", web="https://x")
    p1, created = w.add("v0000", "0001-0100", 1, [(b"a" * 10, ".jpg")], page_title="one")
    assert created
    p2, created = w.add("v0000", "0001-0100", 1, [(b"b" * 10, ".jpg"), (b"c" * 10, ".png")])
    assert p1 == p2 and not created
    with zipfile.ZipFile(p1) as z:
        assert sorted(z.namelist()) == ["00001.jpg", "00002.jpg", "00003.png", "ComicInfo.xml"]
        assert z.read("00001.jpg") == b"a" * 10
        assert b"<Series>Strip</Series>" in z.read("ComicInfo.xml")


def test_epub_build_keeps_text_and_says_it_is_a_book(tmp_path):
    wr = _mod("writers")
    w = wr.EpubWriter(str(tmp_path / "out"), str(tmp_path / "stage"), "Story", web="https://x")
    url = "https://x/comic/ch01/ch01_01.html"
    for i in range(3):
        w.add("ch01", "Chapter 001", url,
              '<div id="comic"><img src="art.png"><p>Words &amp; things</p>'
              '<div style="background:url(bg.png)">x</div><a href="ch01_02.html">next</a></div>',
              {"https://x/comic/ch01/art.png": (png_bytes(seed=i), ".png"),
               "https://x/comic/ch01/bg.png": (png_bytes(seed=10 + i), ".png")}, page_title=f"Page {i + 1}")
    path = w.build("ch01")
    assert os.path.basename(path) == "Story - Chapter 001.epub"
    with zipfile.ZipFile(path) as z:
        assert z.namelist()[0] == "mimetype" and z.read("mimetype") == b"application/epub+zip"
        opf = z.read("OEBPS/content.opf").decode()
        assert 'name="cim:kind" content="book"' in opf and opf.count("<itemref") == 3
        page = z.read("OEBPS/text/page0001.xhtml").decode()
        assert "Words &amp; things" in page and "../img/p0001_01.png" in page and "../img/p0001_02.png" in page
        assert 'href="#"' in page                       # site navigation neutralised
        import xml.dom.minidom
        xml.dom.minidom.parseString(page)               # well-formed XHTML
    bi = None
    for k, m in list(sys.modules.items()):
        if k.endswith("books.book_index"):
            bi = m
    if bi is not None:
        assert bi.epub_is_comic(path) is False          # the hint keeps it in the flowing reader


# -- next-link learning and fallbacks ----------------------------------------------

def _soup(html):
    return _mod("scrape")._soup(html)


def test_next_link_learned_from_two_pages_without_the_word_next():
    nv = _mod("navigate")
    page = lambda i: (f'<html><body><a href="/c/{i - 1}"><img src="/img/arrow_l.png"></a>'
                      f'<a href="/blog">Blog</a><img src="/art/{i}.png">'
                      f'<a href="/c/{i + 1}"><img src="/img/arrow_r.png"></a></body></html>')
    rule, u3 = nv.learn_link(_soup(page(1)), "https://x.test/c/1", _soup(page(2)), "https://x.test/c/2")
    assert rule == {"css": 'a:has(img[src$="arrow_r.png"])'}
    assert u3 == "https://x.test/c/3"
    assert nv.apply_rule(_soup(page(7)), "https://x.test/c/7", rule) == ["https://x.test/c/8"]


def test_pattern_rollover_and_script_scan():
    nv = _mod("navigate")
    u1 = "https://comic.example/story/comic/ch01/ch01_01.html"
    pat = nv.learn_pattern(u1, u1.replace("_01.html", "_02.html"))
    assert pat and pat["chapter"] and pat["step"] == 1
    assert nv.pattern_next(u1.replace("_01.", "_09."), pat).endswith("/ch01/ch01_10.html")
    assert nv.pattern_rollover(u1.replace("_01.", "_40."), pat)[0].endswith("/ch02/ch02_01.html")
    html = '<script>setTimeout(function(){location.href="ch01_06.html"},4000)</script>' \
           '<select><option value="ch01_01.html"><option value="ch01_09.html"></select>'
    assert nv.scan_same_shape(html, u1.replace("_01.", "_05.")).endswith("ch01_06.html")
    jumps, skips = nv.parse_fixes("https://a/1 -> https://a/3; skip https://a/9/")
    assert jumps == {"https://a/1": "https://a/3"} and skips == {"https://a/9"}


# -- a subscription run through the fetch queue -------------------------------------

class _Resp:
    def __init__(self, url, status=200, text="", content=b"", ctype="text/html"):
        self.url, self.status_code, self.text = url, status, text
        self.content = content or text.encode()
        self.headers = {"Content-Type": ctype}


class _Site:
    """! @brief comic.test: pages /p/1../p/N, one PNG each.
    links:   {i: target page} overrides where page i's next arrow points (None = no arrow)
    frames:  pages that are only an animation step of the page before (same art)
    The arrow is an image link, never the word "next"."""

    def __init__(self, n):
        self.n, self.hits, self.links, self.frames = n, [], {}, set()
        self.headers, self.cookies = {}, type("C", (), {"set": lambda *a: None})()

    def art(self, i):
        while i in self.frames:
            i -= 1
        return i

    def get(self, url, headers=None, timeout=None):
        self.hits.append(url)
        if url.startswith("https://comic.test/art/"):
            i = int(url.rsplit("/", 1)[1].split(".")[0])
            return _Resp(url, content=png_bytes(seed=9100 + i), ctype="image/png")
        i = int(url.rstrip("/").rsplit("/", 1)[1])
        if i > self.n or i < 1:
            return _Resp(url, 404)
        to = self.links.get(i, i + 1 if i < self.n else None)
        arrow = f'<a href="/p/{to}"><img src="/ui/go.png"></a>' if to else ""
        return _Resp(url, text=f'<html><head><title>Test Comic - {i}</title></head><body>'
                               f'<div id="comic"><img src="/art/{self.art(i)}.png"></div>'
                               f'<a href="/p/1"><img src="/ui/first.png"></a>{arrow}</body></html>')


@pytest.fixture
def site(app, monkeypatch):
    sc = _mod("scrape")
    s = _Site(3)
    orig = sc.Fetcher.__init__

    def init(self, profile, delay=1.0, timeout=30, session=None):
        orig(self, profile, delay=0, timeout=timeout, session=s)
    monkeypatch.setattr(sc.Fetcher, "__init__", init)
    yield s
    db = app._db()
    db.execute("DELETE FROM import_sources WHERE fetcher='webcomic'")
    db.execute("DELETE FROM fetch_items WHERE fetcher='webcomic'")
    db.execute("DELETE FROM fetch_queue WHERE target LIKE 'webcomic:%'")
    db.execute("DELETE FROM fetch_watch WHERE target LIKE 'webcomic:%'")
    db.commit()
    for r in db.execute("SELECT rel_path FROM books WHERE rel_path LIKE 'webcomic_test/%'").fetchall():
        app.app.test_client().post("/api/delete", json={"filename": r["rel_path"]})
    shutil.rmtree(os.path.join(app.MEDIA_DIR, "webcomic_test"), ignore_errors=True)
    shutil.rmtree(os.path.join(app.MEDIA_DIR, ".webcomic"), ignore_errors=True)


def _run(app, sid):
    reg = app.module_host.get_service("fetch")
    reg.enqueue(f"webcomic:{sid}", "webcomic_test", "webcomic")
    qid = app._db().execute("SELECT MAX(id) FROM fetch_queue WHERE target=?", (f"webcomic:{sid}",)).fetchone()[0]
    return reg.run(qid)


def _follow(client, **cfg):
    base = {"url": "https://comic.test/p/1", "series": "Test Comic", "group": "single",
            "folder": "webcomic_test/{series}", "every_h": 0}
    base.update(cfg)
    j = post_json(client, "/api/import/webcomic/save", {"config": base})
    assert j["ok"], j
    return j["id"]


def _pages(app):
    cbz = os.path.join(app.MEDIA_DIR, "webcomic_test", "Test Comic", "Test Comic.cbz")
    with zipfile.ZipFile(cbz) as z:
        return [z.read(n) for n in sorted(z.namelist()) if n.endswith(".png")]


def _art(i):
    return png_bytes(seed=9100 + i)


def test_subscription_downloads_and_picks_up_new_pages(app, client, ungated, site):
    sid = _follow(client, second_url="https://comic.test/p/2")
    row = _run(app, sid)
    assert row["status"] == "done", row
    assert _pages(app) == [_art(1), _art(2), _art(3)]
    learned = app.module_host.get_service("fetch")  # (state is in the source row)
    src = app._db().execute("SELECT config FROM import_sources WHERE id=?", (sid,)).fetchone()[0]
    assert '"src$=\\"go.png\\"' in src or 'go.png' in src     # learned the arrow, no "next" text

    # nothing new: no art downloaded
    site.hits.clear()
    assert _run(app, sid)["status"] == "done"
    assert not [h for h in site.hits if "/art/" in h]

    # the author posts page 4: the next run appends just that page
    site.n = 4
    site.hits.clear()
    assert _run(app, sid)["status"] == "done"
    assert "https://comic.test/p/1" not in site.hits
    assert _pages(app) == [_art(1), _art(2), _art(3), _art(4)]

    rel = "webcomic_test/Test Comic/Test Comic.cbz"
    book = None
    for _ in range(20):
        book = next((b for b in client.get("/api/books/list").get_json().get("books", [])
                     if b["rel_path"] == rel), None)
        if book and book.get("page_count") == 4:
            break
        time.sleep(0.25)
    assert book and book["kind"] == "comic" and book["page_count"] == 4, book


def test_missing_arrow_and_animation_steps_are_walked_through(app, client, ungated, site):
    site.n = 6
    site.links = {3: None}          # page 3's arrow is JS-only: the URL pattern finds page 4
    site.frames = {5}               # page 5 is the page-4 animation needing another click
    sid = _follow(client, second_url="https://comic.test/p/2")
    assert _run(app, sid)["status"] == "done"
    assert _pages(app) == [_art(1), _art(2), _art(3), _art(4), _art(6)]


def test_repair_puts_a_skipped_page_back_in_order(app, client, ungated, site):
    site.n = 5
    site.links = {2: 4}             # the site's arrow on page 2 skips page 3
    sid = _follow(client, second_url="https://comic.test/p/2")
    assert _run(app, sid)["status"] == "done"
    assert _pages(app) == [_art(1), _art(2), _art(4), _art(5)]
    cfg = __import__("json").loads(app._db().execute(
        "SELECT config FROM import_sources WHERE id=?", (sid,)).fetchone()[0])
    cfg["repair"] = True
    assert post_json(client, "/api/import/webcomic/save", {"id": sid, "config": cfg})["ok"]
    assert _run(app, sid)["status"] == "done"
    assert _pages(app) == [_art(1), _art(2), _art(3), _art(4), _art(5)]


def test_hand_fix_forces_a_jump(app, client, ungated, site):
    site.n = 4
    site.links = {2: None}          # dead end, and no pattern to fall back on (no second page given)
    sid = _follow(client, fixes="https://comic.test/p/2 -> https://comic.test/p/4")
    assert _run(app, sid)["status"] == "done"
    pages = _pages(app)
    assert pages[:2] == [_art(1), _art(2)] and pages[-1] == _art(4)


def test_epub_page_with_unfetched_nested_images_and_extras(tmp_path):
    """! @brief An image that wasn't fetched inside an element that is itself dropped
    (crashed: decomposed tag); insets / backgrounds / srcset in the body are kept."""
    sc, wr = _mod("scrape"), _mod("writers")
    url = "https://comic.example/c/ch02/ch02_10.html"
    html = """<html><body><div id="comic">
      <picture><source srcset="art-2x.webp 2x, art.webp 1x"><img src="art.jpg"></picture>
      <div class="inset" style="background:url(bg.png)"><img src="inset.png"><img src="gone.png"></div>
      <a href="ch02_11.html"><img src="arrow.png"></a></div></body></html>"""
    p = sc.profile_for(url, "fullpage")
    pg = sc.parse_page(html, url, p)
    base = "https://comic.example/c/ch02/"
    assert {base + n for n in ("art-2x.webp", "art.webp", "bg.png", "inset.png")} <= set(pg["images"] + pg["extras"])
    have = {base + n: (png_bytes(seed=i), ".png") for i, n in enumerate(["art.jpg", "art.webp", "bg.png", "inset.png"])}
    w = wr.EpubWriter(str(tmp_path / "out"), str(tmp_path / "stage"), "Story")
    w.add("ch02", "Chapter 002", url, pg["content_html"], have, page_title="10")
    path = w.build("ch02")
    with zipfile.ZipFile(path) as z:
        page = z.read("OEBPS/text/page0001.xhtml").decode()
    assert "gone.png" not in page and "arrow.png" not in page and "srcset" not in page
    assert page.count("../img/") >= 4
    import xml.dom.minidom
    xml.dom.minidom.parseString(page)