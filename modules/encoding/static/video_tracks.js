/* video_tracks.js - the Tracks editor of a video stored as separate streams (encoding module).
 *
 * "Tracks" in the viewer toggles opens it for the current video:
 *   - a single-file video can be split into separate streams here (or stays one file, with why);
 *   - a split video lists its audio and subtitle tracks, each its own file named by its
 *     language ("Movie - english.aac", "Movie - english (commentary).aac"): reorder,
 *     set language / name / default / forced / audio offset, remove, add a track from a
 *     file, or put the video back together as one .mkv.
 * Saving renames the files when a language or name changes their label; languages are
 * never folded together (a second track of one language is "english 2"). */
(function () {
  "use strict";
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const toast = (m) => { if (typeof showToast === "function") showToast(m); else alert(m); };
  const LANGS = ["en", "fr", "de", "es", "it", "pt", "pt-BR", "nl", "sv", "no", "da", "fi", "pl", "cs", "hu",
    "ro", "ru", "uk", "el", "tr", "he", "ar", "fa", "hi", "th", "vi", "id", "zh", "zh-Hans", "zh-Hant",
    "ja", "ko", "und"];
  const S = { rel: "", data: null, el: null };
  const WARNING = "Other players can't use a split video: Plex, Jellyfin, Kodi or a file manager find a " +
    "picture without sound and loose track files. This app plays it, and downloads and Publish rebuild one file.";

  async function json(r) { try { return await r.json(); } catch (e) { return { success: false, error: "HTTP " + r.status }; } }
  async function post(url, body) {
    return json(await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                                   body: JSON.stringify(body || {}) }));
  }
  function size(n) {
    if (n == null) return "missing";
    const u = ["B", "KB", "MB", "GB"]; let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i ? n.toFixed(1) : n) + " " + u[i];
  }

  function close() { if (S.el) S.el.remove(); S.el = null; }

  function shell(inner) {
    close();
    const el = document.createElement("div");
    el.id = "vt_modal";
    // layout inline: the modal must sit right whatever the built stylesheet holds
    el.style.cssText = "position:fixed;inset:0;z-index:80;background:rgba(0,0,0,.7);display:flex;" +
      "align-items:center;justify-content:center;padding:16px";
    el.innerHTML = `<div class="bg-gray-900 border border-gray-700 rounded-lg p-4 text-sm"
        style="width:100%;max-width:52rem;max-height:90vh;overflow-y:auto">
      <div class="flex items-center gap-2 mb-2"><h2 class="font-bold text-sky-300">Tracks</h2>
        <span class="text-xs text-gray-400 truncate">${esc(S.rel)}</span>
        <button type="button" data-vt="close" class="ml-auto px-2 py-0.5 rounded bg-gray-700 hover:bg-gray-600">Close</button></div>
      <div id="vt_body">${inner}</div></div>`;
    el.addEventListener("click", (ev) => { if (ev.target === el || ev.target.closest('[data-vt="close"]')) close(); });
    document.body.appendChild(el);
    S.el = el;
    return el;
  }

  function trackRow(t, i, n) {
    const audio = t.type === "audio";
    return `<tr class="border-b border-gray-800 align-top" data-i="${i}">
      <td class="py-1 pr-2"><span class="px-1 rounded ${audio ? "bg-indigo-900 text-indigo-200" : "bg-emerald-900 text-emerald-200"}">${audio ? "audio" : "subs"}</span>
        <div class="text-[10px] text-gray-500 mt-0.5" title="${esc(t.codec)}">${esc(size(t.size))}</div></td>
      <td class="py-1 pr-2"><div class="text-xs text-gray-200 break-all" title="The file">${esc(t.file)}</div></td>
      <td class="py-1 pr-2"><input data-k="language" list="vt_langs" value="${esc(t.language)}" class="w-20 bg-gray-800 border border-gray-700 rounded px-1"
        title="Language code (BCP 47): en, fr, pt-BR, zh-Hant ... - the file is named after it"></td>
      <td class="py-1 pr-2"><input data-k="name" value="${esc(t.name)}" placeholder="name" class="w-32 bg-gray-800 border border-gray-700 rounded px-1"
        title="Track name (commentary, SDH, ...) - added to the file name"></td>
      <td class="py-1 pr-2 whitespace-nowrap text-xs">
        <label title="Played / shown first"><input type="checkbox" data-k="default" ${t.default ? "checked" : ""}> default</label>
        ${audio ? "" : `<br><label title="Always shown (signs, foreign dialogue)"><input type="checkbox" data-k="forced" ${t.forced ? "checked" : ""}> forced</label>`}
        ${audio ? `<br><label title="Start offset of this audio, in milliseconds">offset <input data-k="start_ms" type="number" step="1" value="${Math.round(t.start_ms || 0)}" class="w-16 bg-gray-800 border border-gray-700 rounded px-1"> ms</label>` : ""}</td>
      <td class="py-1 whitespace-nowrap">
        <button type="button" data-vt="up" ${i === 0 ? "disabled" : ""} class="px-1 rounded bg-gray-700 disabled:opacity-30" title="Move up">↑</button>
        <button type="button" data-vt="down" ${i === n - 1 ? "disabled" : ""} class="px-1 rounded bg-gray-700 disabled:opacity-30" title="Move down">↓</button>
        <button type="button" data-vt="remove" class="px-1 rounded bg-red-900 hover:bg-red-800" title="Remove this track (its file is deleted on Save)">×</button></td></tr>`;
  }

  function render() {
    const d = S.data;
    if (!d.split) {
      const why = !d.available ? "Splitting needs mkvtoolnix (mkvmerge, mkvextract) on the server."
        : d.public ? "It is in a public movies / tv / music folder: Plex and Jellyfin need single files." : "";
      shell(`<p class="text-gray-300 mb-2">This video is one file.</p>
        ${why ? `<p class="text-xs text-amber-300">${esc(why)}</p>`
              : `<button type="button" data-vt="split" class="px-2 py-1 rounded bg-sky-700 hover:bg-sky-600">Split into separate streams</button>
                 <p class="text-xs text-gray-500 mt-1">The picture stays as the video, each audio and subtitle track becomes its own file named by its language. It is only split when the parts rebuild the file exactly.</p>
                 <p class="text-xs text-amber-400 mt-1">\u26a0 ${esc(WARNING)}</p>`}`);
      const b = S.el.querySelector('[data-vt="split"]');
      if (b) b.addEventListener("click", split);
      return;
    }
    const v = d.video;
    const rows = d.tracks.map((t, i) => trackRow(t, i, d.tracks.length)).join("");
    shell(`
      <datalist id="vt_langs">${LANGS.map((l) => `<option value="${l}">`).join("")}</datalist>
      <p class="text-[11px] text-amber-400 mb-1">\u26a0 ${esc(WARNING)}</p>
      <div class="text-xs text-gray-400 mb-2">Picture: <span class="text-gray-200">${esc(v.file)}</span>
        (${esc(v.codec)}, ${esc(v.pixel_dimensions)}, ${esc(size(v.size))})
        ${d.chapters ? " | chapters" : ""}${d.attachments.length ? ` | ${d.attachments.length} attachment(s)` : ""}</div>
      <label class="text-xs text-gray-400">Title <input id="vt_title" value="${esc(d.title)}" class="w-80 bg-gray-800 border border-gray-700 rounded px-1 ml-1"></label>
      <table class="w-full mt-2 text-sm"><thead><tr class="text-left text-[11px] text-gray-500">
        <th>Kind</th><th>File</th><th>Language</th><th>Name</th><th>Flags</th><th></th></tr></thead>
        <tbody id="vt_rows">${rows || '<tr><td colspan="6" class="text-xs text-gray-500 py-2">No audio or subtitle tracks.</td></tr>'}</tbody></table>
      <div class="flex flex-wrap items-center gap-2 mt-3">
        <button type="button" data-vt="save" class="px-3 py-1 rounded bg-green-700 hover:bg-green-600 font-bold">Save</button>
        <button type="button" data-vt="reload" class="px-2 py-1 rounded bg-gray-700 hover:bg-gray-600">Undo changes</button>
        <button type="button" data-vt="join" class="ml-auto px-2 py-1 rounded bg-gray-700 hover:bg-gray-600"
          title="Rebuild this video as one .mkv with every track in it">Put back together</button></div>
      <fieldset class="mt-3 border border-gray-700 rounded p-2"><legend class="text-xs text-sky-300 px-1">Add a track</legend>
        <div class="flex flex-wrap items-center gap-2 text-xs">
          <input type="file" id="vt_file" accept=".aac,.ac3,.eac3,.dts,.thd,.flac,.mp3,.mp2,.opus,.ogg,.wav,.m4a,.mka,.srt,.ass,.ssa,.vtt,.sup,.mks">
          <input id="vt_lang" list="vt_langs" placeholder="language (en)" class="w-24 bg-gray-800 border border-gray-700 rounded px-1">
          <input id="vt_name" placeholder="name (optional)" class="w-32 bg-gray-800 border border-gray-700 rounded px-1">
          <label><input type="checkbox" id="vt_default"> default</label>
          <label><input type="checkbox" id="vt_forced"> forced</label>
          <button type="button" data-vt="add" class="px-2 py-0.5 rounded bg-sky-700 hover:bg-sky-600">Add</button></div>
        <p class="text-[11px] text-gray-500 mt-1">One audio or subtitle track per file; a .m4a / .mka / .mks is reduced to its raw stream. It is checked against the video before it is kept.</p>
      </fieldset>`);
    const body = S.el.querySelector("#vt_body");
    body.addEventListener("input", (ev) => {
      const tr = ev.target.closest("tr[data-i]"); const k = ev.target.dataset.k;
      if (!tr || !k) return;
      const t = d.tracks[+tr.dataset.i];
      t[k] = ev.target.type === "checkbox" ? ev.target.checked : ev.target.value;
      if (k === "default" && t.default) d.tracks.forEach((o) => { if (o !== t && o.type === t.type) o.default = false; });
      if (k === "default") render();
    });
    body.addEventListener("click", (ev) => {
      const b = ev.target.closest("button[data-vt]"); if (!b) return;
      const act = b.dataset.vt, tr = b.closest("tr[data-i]"), i = tr ? +tr.dataset.i : -1;
      if (act === "up" && i > 0) { [d.tracks[i - 1], d.tracks[i]] = [d.tracks[i], d.tracks[i - 1]]; render(); }
      else if (act === "down" && i >= 0 && i < d.tracks.length - 1) { [d.tracks[i + 1], d.tracks[i]] = [d.tracks[i], d.tracks[i + 1]]; render(); }
      else if (act === "remove" && i >= 0) { d.tracks.splice(i, 1); render(); }
      else if (act === "save") save();
      else if (act === "reload") load(S.rel);
      else if (act === "join") join();
      else if (act === "add") add();
    });
    const title = S.el.querySelector("#vt_title");
    if (title) title.addEventListener("input", () => { d.title = title.value; });
  }

  async function load(rel) {
    S.rel = rel;
    const d = await json(await fetch("/api/encoding/streams/tracks?rel=" + encodeURIComponent(rel)));
    if (!d.success) { toast(d.error || "Tracks unavailable"); close(); return; }
    S.data = d;
    if (d.split) d._orig = d.tracks.map((t) => t.file);
    render();
  }

  async function save() {
    const d = S.data;
    const removed = S.data._orig.filter((f) => !d.tracks.some((t) => t.file === f));
    if (removed.length && !confirm(`Delete ${removed.length} track file(s)?\n\n${removed.join("\n")}`)) return;
    const r = await post("/api/encoding/streams/tracks/save", { rel: S.rel, title: d.title,
      tracks: d.tracks.map((t) => ({ file: t.file, language: t.language, name: t.name, default: !!t.default,
                                     forced: !!t.forced, start_ms: Number(t.start_ms) || 0 })) });
    if (!r.success) { toast(r.error || "Save failed"); return; }
    toast("Tracks saved.");
    S.data = r; S.data._orig = r.tracks.map((t) => t.file);
    render();
  }

  async function add() {
    const f = S.el.querySelector("#vt_file").files[0];
    if (!f) { toast("Choose a file first."); return; }
    const fd = new FormData();
    fd.append("rel", S.rel); fd.append("file", f);
    fd.append("language", S.el.querySelector("#vt_lang").value || "und");
    fd.append("name", S.el.querySelector("#vt_name").value || "");
    fd.append("default", S.el.querySelector("#vt_default").checked ? "1" : "0");
    fd.append("forced", S.el.querySelector("#vt_forced").checked ? "1" : "0");
    const r = await json(await fetch("/api/encoding/streams/tracks/add", { method: "POST", body: fd }));
    if (!r.success) { toast(r.error || "Add failed"); return; }
    toast("Track added.");
    S.data = r; S.data._orig = r.tracks.map((t) => t.file);
    render();
  }

  async function changed(rel) {
    if (typeof loadGallery === "function") loadGallery();
    if (rel && typeof selectFile === "function") { try { selectFile(rel); } catch (e) { /* not in view */ } }
  }

  async function split() {
    if (!confirm("Split this video into separate streams?\n\n" + WARNING)) return;
    toast("Splitting... (checking that the parts rebuild the file)");
    const r = await post("/api/encoding/streams/one", { rel: S.rel, action: "demux" });
    if (!r.success) { toast("Stays one file: " + (r.error || "failed")); return; }
    await changed(r.rel);
    load(r.rel);
  }

  async function join() {
    if (!confirm("Put this video back together as one .mkv? Its track files are removed.")) return;
    const r = await post("/api/encoding/streams/one", { rel: S.rel, action: "mux" });
    if (!r.success) { toast(r.error || "Failed"); return; }
    close();
    toast("Put back together: " + r.rel);
    changed(r.rel);
  }

  window.videoTracksOpen = async function (rel) {
    rel = rel || window.currentFile;
    if (!rel) { toast("Open a video first."); return; }
    await load(rel);
  };

  function register() {
    if (window.registerControlButton) {
      registerControlButton("viewer_toggles", { label: "Tracks", onclick: "videoTracksOpen()", variant: "neutral",
        feature: "data.move", title: "Audio and subtitle tracks of this video (separate streams)" });
    }
  }
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && S.el) close(); });
  if (document.readyState === "loading") window.addEventListener("DOMContentLoaded", register);
  else register();
})();