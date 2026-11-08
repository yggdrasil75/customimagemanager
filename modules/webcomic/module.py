"""! @file
@brief Webcomic module - follow a webcomic and keep it downloaded as a comic or book.
======================================================================

A subscription is a start URL. Each run walks the comic page by page from
where the last run stopped (the page's "next" link), downloads the art, and
grows the output in place:

    cbz   one archive per chapter / episode / volume of N strips; each new page
          is appended straight into the archive (no extract-and-rebuild), and
          the shelf is refreshed when the run ends
    epub  the whole page body (art plus the text and layout around it) per
          page, one book per chapter - for comics whose pages are more than
          one picture (text around the art, layouts, animations)

Three styles: webtoon (vertical episodes), full page (the whole page as
EPUB) and generic (page images as CBZ). Each uses heuristics (largest image,
a learned next link) unless you give CSS selectors. Runs go through the fetch queue (progress, cancel, retries,
the low-disk pause) and a check interval makes it a fetch watch, so new
pages arrive on their own.

UI: Settings > Webcomics (the fetch module's importer tab).
"""

import json
import os
from collections import OrderedDict

from modules.fetch.importing import Importer

from . import navigate as nav
from . import scrape
from .writers import CbzWriter, EpubWriter, ext_for, safe_name

MANIFEST = {
    "id":          "webcomic",
    "name":        "Webcomics",
    "version":     "1.0.0",
    "description": "Follow webcomics (webtoons, full-page comics, newspaper-style strips) and keep them "
                   "downloaded as .cbz comics or .epub books that grow as new pages come out.",
    "core":        False,
    "requires":    ["fetch", "books", "comics"],
    "pip":         ["requests", "beautifulsoup4:bs4"],
    "pip_optional": ["lxml"],
}

FETCHER = "webcomic"
TAB = "webcomic"

_PROFILE_OPTS = [["auto", "Webtoon if the site is one, else generic"]] + [
    [k, v["label"]] for k, v in scrape.PROFILES.items()]


def _expand_folder(tpl, series):
    tpl = (tpl or "comics/{series}").strip().strip("/")
    return "/".join(safe_name(p.replace("{series}", series), "comics") for p in tpl.split("/") if p)


def register(host):
    if host.get_service("fetch") is None:
        return
    fetch = host.get_service("fetch")

    host.add_import_tab(
        TAB, "Webcomics", fetcher=FETCHER, title="Webcomic subscriptions",
        sources_label="Webcomics", add_label="Follow a webcomic", icon="\U0001f4d6", common=False,
        intro="Paste the URL of the page to start from (usually page 1) and, ideally, the page after it: the "
              "<i>next</i> link is learned from the two and checked on the second, and the URL pattern "
              "(page 41 -> 42, chapter rollover) backs it up when a button is missing or needs extra clicks. "
              "Each run continues from the last page it got and adds the new pages to the comic in place. "
              "<i>Style</i>: <i>Webtoon</i> for vertical episodes, <i>Full page</i> when the page around the art "
              "matters (kept as .epub), <i>Generic</i> for page images and strips. "
              "Art is found as the largest image unless you give CSS selectors. "
              "Set <i>Check every</i> to pick up new pages automatically.",
        fields=[
            {"key": "url", "label": "Start at (page URL)", "kind": "text",
             "placeholder": "https://example.com/comic/page-1"},
            {"key": "second_url", "label": "Second page URL (recommended)", "kind": "text", "default": "",
             "help": "the page after the first: the next link is learned from these two pages and "
                     "checked on the second, so the button needn't say \"next\""},
            {"key": "series", "label": "Name", "kind": "text", "default": "",
             "help": "empty = the site's title"},
            {"key": "profile", "label": "Style", "kind": "select", "default": "auto", "options": _PROFILE_OPTS},
            {"key": "output", "label": "Save as", "kind": "select", "default": "auto",
             "options": [["auto", "Style default"], ["cbz", "Comic (.cbz, pages are images)"],
                         ["epub", "Book (.epub, keeps the whole page)"]]},
            {"key": "group", "label": "One file per", "kind": "select", "default": "auto",
             "options": [["auto", "Style default"], ["chapter", "Chapter (any length)"],
                         ["episode", "Episode"], ["volume", "Fixed number of pages"],
                         ["single", "Whole comic, one file"]]},
            {"key": "chapter_sel", "label": "Chapter marker (CSS, optional)", "kind": "text", "default": "",
             "help": "an element whose text names the chapter, e.g. <i>.chapter-title</i> or the chapter "
                     "drop-down <i>select#chapters</i>. Empty = found from the URL, the page title and headings; "
                     "a page without a marker stays in the chapter before it"},
            {"key": "volume_size", "label": "Pages per file (fixed / fallback)", "kind": "number", "default": 100,
             "help": "for <i>Fixed number of pages</i>, and for chapter grouping until a first chapter "
                     "marker is found"},
            {"key": "folder", "label": "Put files in", "kind": "text", "default": "comics/{series}",
             "help": "library folder; {series} = the name"},
            {"key": "every_h", "label": "Check for new pages every (hours)", "kind": "number", "default": 24,
             "help": "0 = only when you press Run"},
            {"key": "max_pages", "label": "Max pages per run", "kind": "number", "default": 0,
             "help": "0 = no limit (a first run of a long comic takes a while)"},
            {"key": "delay", "label": "Seconds between requests", "kind": "number", "default": 1.5},
            {"key": "sel_images", "label": "Art selector (CSS)", "kind": "text", "default": "",
             "help": "optional, e.g. #comic img"},
            {"key": "sel_next", "label": "Next link selector (CSS)", "kind": "text", "default": "",
             "help": "optional, e.g. a.next"},
            {"key": "sel_content", "label": "Page body selector (CSS, epub)", "kind": "text", "default": "",
             "help": "optional: what an .epub page keeps"},
            {"key": "fixes", "label": "Hand fixes", "kind": "text", "default": "",
             "help": "for pages no rule gets right: <i>URL -> URL</i> forces where a page leads, "
                     "<i>skip URL</i> leaves a page out; separate with ;"},
            {"key": "repair", "label": "Repair on the next run: look for pages missed between saved ones",
             "kind": "toggle", "default": False},
            {"key": "relearn", "label": "Re-learn the next link on the next run", "kind": "toggle",
             "default": False},
            {"key": "restart", "label": "Start over from the start URL on the next run", "kind": "toggle",
             "default": False},
        ])

    def _profile(cfg):
        url = cfg.get("url") or ""
        pid = cfg.get("profile") if cfg.get("profile") not in (None, "", "auto") else None
        ov = {"images": cfg.get("sel_images") or None, "next": cfg.get("sel_next") or None,
              "content": cfg.get("sel_content") or None, "chapter_sel": cfg.get("chapter_sel") or None}
        if cfg.get("output") in ("cbz", "epub"):
            ov["output"] = cfg["output"]
        if cfg.get("group") not in (None, "", "auto"):
            ov["group"] = cfg["group"]
        if cfg.get("volume_size"):
            ov["volume_size"] = int(float(cfg["volume_size"]) or 100)
        return scrape.merged_profile(url, pid, ov)

    def _validate(cfg, secrets, sid):
        url = str(cfg.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("the start URL must begin with http:// or https://")
        cfg["url"] = url
        prev = imp.source(sid) if sid else None
        state = dict((prev or {}).get("config", {}).get("state") or {})
        if prev and prev["config"].get("url") != url:
            state = {}                              # a different comic: start fresh
        if prev and (prev["config"].get("second_url") or "") != (cfg.get("second_url") or ""):
            state.pop("learned", None)              # new second page: learn again
        if cfg.pop("relearn", False):
            state.pop("learned", None)
        cfg["relearn"] = False
        if cfg.pop("restart", False):
            state = {}
        cfg["state"] = state
        cfg["restart"] = False
        p = _profile(cfg)
        label = (cfg.get("series") or "").strip() or state.get("series") or \
            (url.split("//", 1)[-1].split("/")[0] + f" ({p['id']})")
        return cfg, secrets, label, None

    imp = Importer(host, FETCHER, validate=_validate, default_folder="comics/{series}")

    def _media_abs():
        # host.media_dir may be relative; safe_path resolves it the way file paths are
        return host.safe_path(host.media_dir, ".") or os.path.abspath(host.media_dir)

    def _rel(path):
        return os.path.relpath(path, _media_abs()).replace(os.sep, "/")

    def _index(rel):
        svc = host.get_service("books") or {}
        fn = svc.get("index_one")
        if fn:
            try:
                fn(rel)
            except Exception as e:
                host.logger.warning(f"webcomic: indexing {rel}: {e}")

    def _fetch(target, tmpdir, on_file, ctx=None):
        src = imp.source(target)
        if src is None:
            raise RuntimeError("this webcomic subscription was removed")
        cfg = src["config"]
        st = dict(cfg.get("state") or {})
        ctx.scope = f"{src['id']}"
        prof = _profile(cfg)
        out_kind = prof.get("output") or "cbz"
        fetcher = scrape.Fetcher(prof, delay=float(cfg.get("delay") or 1.5))
        max_pages = int(float(cfg.get("max_pages") or 0))
        jumps, skips = nav.parse_fixes(cfg.get("fixes"))
        repair = bool(cfg.get("repair"))
        series = (cfg.get("series") or "").strip() or st.get("series") or ""
        writer, touched, open_groups = None, set(), set()
        got, fetched = 0, OrderedDict()             # small LRU of parsed pages

        def save_state():
            cfg["state"] = st
            if repair:
                cfg["repair"] = False                  # one-shot, like "start over"
            imp.update_source(src["id"], config=cfg,
                              label=(cfg.get("series") or "").strip() or st.get("series") or src["label"])

        def load(url):
            """! @brief GET + parse, cached for the run. None when it isn't a page."""
            k = url.split("#")[0]
            if k in fetched:
                fetched.move_to_end(k)
                return fetched[k]
            r = fetcher.get(url)
            page = scrape.parse_page(r.text, r.url or url, prof) if r.status_code == 200 else None
            fetched[k] = page
            while len(fetched) > 24:
                fetched.popitem(last=False)
            return page

        def pick_next(url, page, avoid=(), prefer_pattern=False):
            """! @brief The first candidate for page N+1 that is a real page.
            Guessed URLs (pattern / script / next chapter) must carry art."""
            cands = nav.candidates(page["_html"], page["_soup"], url, page["next"],
                                   st.get("learned"), jumps)
            if prefer_pattern:
                cands.sort(key=lambda c: c[1] not in ("hand fix", "URL pattern", "next chapter"))
            for u, why in cands:
                if any(nav.same_page(u, a) for a in avoid):
                    continue
                p = load(u)
                if p is None:
                    continue
                if not p["images"] and why in ("URL pattern", "page script", "next chapter"):
                    continue
                return u, p, why
            return None

        def step(url, page, avoid=(), prefer_pattern=False):
            """! @brief pick_next, walking through in-between states: a page showing
            the same art as this one (an animation step that needs another click)
            or no art at all (an interstitial), up to a few hops."""
            cur_imgs, hop, seen_hop = page["images"], 0, {url.split("#")[0]}
            nxt = pick_next(url, page, avoid, prefer_pattern)
            while nxt and hop < 6 and (not nxt[1]["images"] or nxt[1]["images"] == cur_imgs or
                                        nxt[0].split("#")[0].rstrip("/") in skips):
                if nxt[0].split("#")[0] in seen_hop:
                    return None
                seen_hop.add(nxt[0].split("#")[0])
                hop += 1
                nxt = pick_next(nxt[0], nxt[1], list(avoid) + list(seen_hop), prefer_pattern)
            if nxt and (not nxt[1]["images"] or nxt[1]["images"] == cur_imgs):
                return None
            return nxt

        def learn(u1, p1, u2=None, p2=None):
            """! @brief Two pages -> which link is "next", and the URL pattern."""
            if u2 is None:
                first = pick_next(u1, p1)
                if not first:
                    return {"link": None, "pattern": None, "verified": False}
                u2, p2 = first[0], first[1]
            rule, u3 = nav.learn_link(p1["_soup"], u1, p2["_soup"], u2)
            pat = nav.learn_pattern(u1, u2)
            out = {"link": rule, "pattern": pat, "from": [u1, u2], "third": u3,
                   "verified": bool(rule)}
            ctx.message("learned the next link (" + (str(rule) if rule else "none") + "), "
                        "URL pattern " + ("found" if pat else "none"))
            return out

        def make_writer():
            nonlocal writer
            folder_rel = _expand_folder(cfg.get("folder"), series)
            folder_abs = host.safe_path(host.media_dir, folder_rel)
            if not folder_abs:
                raise RuntimeError(f"bad folder: {folder_rel}")
            stage = os.path.join(_media_abs(), ".webcomic", str(src["id"]))
            if out_kind == "epub":
                writer = EpubWriter(folder_abs, stage, series, web=cfg["url"])
            else:
                writer = CbzWriter(folder_abs, series, web=cfg["url"], stage=stage)

        def finish_group(key):
            if out_kind == "epub" and key in open_groups:
                path = writer.build(key)
                open_groups.discard(key)
                if path:
                    touched.add(path)

        def download(page, url):
            imgs, assets = [], {}
            urls = page["images"] + ([f for f in page.get("frames", []) if f not in page["images"]]
                                     if out_kind == "epub" else [])
            for iu in urls:
                ir = fetcher.get(iu, referer=prof.get("referer") or url)
                ct = ir.headers.get("Content-Type", "")
                bad = ir.status_code != 200 or ct.startswith("text/") or \
                    ir.content[:15].lstrip().lower().startswith((b"<!doctype", b"<html"))
                if bad:
                    if iu not in page["images"]:
                        continue                      # an animation frame that isn't there: fine
                    raise RuntimeError(f"image {iu}: HTTP {ir.status_code} {ct} "
                                       "(hot-link protection? the site may need a Referer)")
                e = ext_for(iu, ct)
                assets[iu] = (ir.content, e)
                if iu in page["images"]:
                    imgs.append((ir.content, e))
            return imgs, assets

        def group_for(url, idx, page=None, prev=None):
            """! @brief The archive this page goes in; a page with no chapter marker
            stays in `prev` (chapters are any length)."""
            return scrape.group_of(prof, url, idx, page=page, learned=st.get("learned"), prev=prev)

        def note_group(gkey, gtitle):
            groups = st.setdefault("groups", [])
            if not any(g[0] == gkey for g in groups):
                groups.append([gkey, gtitle])

        def store(url, page, gkey, gtitle, after=None):
            """! @brief Write one page into its archive. after = repair insertion point."""
            imgs, assets = download(page, url)
            if out_kind == "epub":
                content = page["content_html"] or "".join(f'<img src="{u}"/>' for u in page["images"])
                for f in page.get("frames", []):
                    if f in assets and f not in page["images"] and f not in content:
                        content += f'<img src="{f}"/>'
                writer.add(gkey, gtitle, url, content, assets, page_title=page["title"], after_url=after)
                open_groups.add(gkey)
                path = writer.path_for(gtitle)
            else:
                num = (int(gkey[1:]) + 1) if gkey.startswith("v") and gkey[1:].isdigit() \
                    else (scrape._num(gkey) or None)
                if after:
                    path = writer.insert_after(gkey, gtitle, after, url, imgs, page_title=page["title"], number=num)
                else:
                    path, _ = writer.add(gkey, gtitle, num, imgs, page_title=page["title"], url=url)
                touched.add(path)
            note_group(gkey, gtitle)
            ctx.mark(url, "done", rel_path=_rel(path), name=(page["title"] or url)[:200])
            return scrape.sha1(b"".join(d for d, _ in imgs))

        def known_order():
            """! @brief Every saved page URL in reading order: [(url, gkey, gtitle)]."""
            out = []
            for gkey, gtitle in st.get("groups", []):
                if out_kind == "epub":
                    mp = os.path.join(writer._gdir(gkey), "group.json")
                    pages = json.load(open(mp))["pages"] if os.path.exists(mp) else []
                else:
                    pages = writer.manifest(gkey)["pages"]
                out += [(p["url"], gkey, gtitle) for p in pages if p.get("url")]
            return out

        def do_repair():
            """! @brief Look between every two saved pages for pages the walk missed
            (a next button that skipped, a chapter end) and slot them in."""
            order = known_order()
            known = {u.split("#")[0] for u, _, _ in order}
            added = 0
            for i in range(len(order) - 1):
                if ctx.stopping():
                    break
                a, gk, gt = order[i]
                b = order[i + 1][0]
                pat = (st.get("learned") or {}).get("pattern")
                if pat and nav.same_page(nav.pattern_next(a, pat), b):
                    continue                         # A+1 is B: nothing missing here
                ctx.message(f"repair: checking after {a}")
                pa = load(a)
                if pa is None:
                    continue
                prev, cur, n = a, step(a, pa, prefer_pattern=True), 0
                while cur and n < 60 and not nav.same_page(cur[0], b) and cur[0].split("#")[0] not in known:
                    g2 = group_for(cur[0], int(st.get("pages") or 0), cur[1], prev=(gk, gt))
                    gk, gt = g2
                    store(cur[0], cur[1], g2[0], g2[1], after=prev)
                    known.add(cur[0].split("#")[0])
                    st["pages"] = int(st.get("pages") or 0) + 1
                    added += 1
                    n += 1
                    prev = cur[0]
                    cur = step(cur[0], cur[1], prefer_pattern=True)
            return added

        # -- start: first run learns the next link from two pages ------------------
        try:
            if st.get("last_url"):
                url, page = st["last_url"], None
                ctx.message("checking for new pages...")
                page = load(url)
                if page is None:
                    raise RuntimeError(f"the last page we got no longer loads: {url}")
                resume = True
            else:
                url = cfg["url"]
                page = load(url)
                if page is None:
                    raise RuntimeError(f"{url} did not load")
                if not page["images"]:
                    raise RuntimeError(f"no comic image found on {url} - set an art selector")
                resume = False
            if not series:
                series = safe_name(page["series"] or page["title"].split("|")[0].split(" - ")[0], "Webcomic")
                st["series"] = series
            make_writer()
            if not st.get("learned"):
                second = (cfg.get("second_url") or "").strip()
                p2 = load(second) if second else None
                if second and p2 is None:
                    raise RuntimeError(f"the second page URL did not load: {second}")
                st["learned"] = learn(url, page, second or None, p2)
                if second and not st["learned"]["link"]:
                    ctx.message("no link on page 1 leads to the second page you gave: "
                                "following the URL pattern and page scripts instead")
            if repair:
                n = do_repair()
                ctx.message(f"repair: {n} missed page(s) put back")
                got += n

            # -- walk forward --------------------------------------------------------
            visited = {url.split("#")[0]}             # never walk back into this run's pages
            if not resume:
                gkey, gtitle = group_for(url, 0, page)
                if not ctx.seen(url):
                    st["last_hash"] = store(url, page, gkey, gtitle)
                    st["pages"] = 1
                    got += 1
                st["group"], st["group_title"], st["last_url"] = gkey, gtitle, url
                save_state()
                yield (None, {})
            while not ctx.stopping():
                if max_pages and got >= max_pages:
                    ctx.message(f"stopped after {got} pages (max per run); the next run continues")
                    break
                nxt = step(url, page, avoid=visited)
                if not nxt:
                    break                              # caught up
                url, page, why = nxt
                visited.add(url.split("#")[0])
                idx = int(st.get("pages") or 0)
                gkey, gtitle = group_for(url, idx, page,
                                         prev=(st["group"], st.get("group_title")) if st.get("group") else None)
                if st.get("group") and st["group"] != gkey:
                    finish_group(st["group"])
                if url.split("#")[0].rstrip("/") in skips:
                    ctx.skip(url, "skipped by a hand fix")
                elif not ctx.seen(url):
                    h = store(url, page, gkey, gtitle)
                    if h == st.get("last_hash"):
                        ctx.message("the site served the same art again: caught up")
                        break
                    st["last_hash"] = h
                    st["pages"] = idx + 1
                    got += 1
                st["group"], st["group_title"], st["last_url"] = gkey, gtitle, url
                save_state()
                ctx.message(f"{series}: page {st.get('pages', 0)} ({gtitle or 'all'}, via {why}) "
                            f"- {got} new this run")
                yield (None, {})
        finally:
            if writer is not None:
                for k in list(open_groups):
                    finish_group(k)
            save_state()
            for path in touched:
                _index(_rel(path))
            if got:
                host.set_status(f"Webcomic {series}: {got} new page(s)")

    fetch.register({"id": FETCHER, "url": False, "label": "Webcomic", "available": lambda: True,
                    "handles": lambda t: str(t).startswith(FETCHER + ":"), "target_key": lambda t: t,
                    "fetch": _fetch})