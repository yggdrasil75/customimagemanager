/* Semantic rank / filter switch (embedding module).
 *
 * `sem:<text>` (or `~<text>`) ranks the library by similarity: the grid shows
 * best first, and nothing else can order or scope it. `about:<text>` is the
 * same match as an ordinary search token: a filter that keeps the files above
 * the similarity threshold and leaves the order to sort:, so the timeline,
 * select-all and other tokens work with it. A search token holds no spaces, so
 * about: joins words with "_".
 *
 * A small button inside the search box (ext area "search_tools") shows while
 * the box holds either form and rewrites one into the other. */
(function () {
  "use strict";

  const ABOUT_RE = /(^|\s)about:(\S+)/i;

  /** @brief The semantic form the query holds: {mode: "rank"|"filter", text} or null. */
  function parse(q) {
    q = (q || "").trim();
    let m = q.match(/^sem:\s*(.*)$/i);
    if (m) return { mode: "rank", text: m[1].trim() };
    if (q.startsWith("~")) return { mode: "rank", text: q.slice(1).trim() };
    m = q.match(ABOUT_RE);
    if (m) return { mode: "filter", text: m[2].replace(/[_+]/g, " ").trim() };
    return null;
  }

  /** @brief The other form of a semantic query, or null when it holds none.
   *  rank -> filter keeps the words (joined by "_"); filter -> rank keeps only the
   *  about: text, since a ranked search can't carry other tokens. */
  function toggled(q) {
    const p = parse(q);
    if (!p || !p.text) return null;
    if (p.mode === "rank") return "about:" + p.text.split(/\s+/).join("_");
    return "sem:" + p.text;
  }

  /** @brief Show / label the switch for what the search box holds now. */
  function sync() {
    const btn = document.getElementById("sem_mode_btn");
    const si = document.getElementById("search_input");
    if (!btn || !si) return;
    const p = parse(si.value);
    btn.style.display = p && p.text ? "" : "none";
    if (!p) return;
    const rank = p.mode === "rank";
    btn.textContent = rank ? "Filter" : "Rank";
    btn.title = rank
      ? "Ranked by similarity. Switch to a filter (about:): keeps the matches in the normal sort order; works with the timeline, select-all and other tokens"
      : "Filtered by similarity (about:). Switch to ranking (sem:): best match first; other tokens are dropped";
  }

  /** @brief Rewrite the search box to the other form and run the search. */
  function flip() {
    const si = document.getElementById("search_input");
    if (!si) return;
    const next = toggled(si.value);
    if (next == null) return;
    si.value = next;
    si.dispatchEvent(new Event("input", { bubbles: true }));
    sync();
  }

  window.CIMSemanticMode = { parse, toggled, flip };

  if (window.registerControlButton) {
    registerControlButton("search_tools", {
      label: "Filter", id: "sem_mode_btn", variant: "neutral", hidden: true,
      title: "Semantic search: rank / filter",
    });
  }
  document.addEventListener("click", e => {
    if (e.target instanceof Element && e.target.closest("#sem_mode_btn")) flip();
  });
  document.addEventListener("input", e => { if (e.target && e.target.id === "search_input") sync(); });
  if (document.readyState === "loading") window.addEventListener("DOMContentLoaded", sync);
  else sync();
})();
