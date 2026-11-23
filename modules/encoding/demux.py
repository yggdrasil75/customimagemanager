"""! @file
@brief Videos stored as separate streams (Settings -> Media -> Video -> "Store as
separate streams"), and their tracks managed one by one.
======================================================================
A split video is a set of files side by side:

    Movie.avc                     the picture: raw H.264 (.hevc H.265, .ivf VP8 / VP9,
                                  .obu AV1, .m2v MPEG-1/2) - the library item
    Movie.m3u8                    the manifest, in HLS master-playlist syntax
    Movie - english.aac           one file per audio track, named by its language
    Movie - japanese.aac
    Movie - english (commentary).opus
    Movie - english.srt           one file per subtitle track
    Movie - english (forced).srt
    Movie.timestamps.txt          every frame's time (Matroska "timestamp v2")
    Movie.chapters.xml            chapters, when there are any
    Movie.tags.xml                tags, when there are any
    Movie - attachment - font.ttf attachments (ASS fonts, cover art)

A language is never folded into another: two tracks of one language are told
apart by their track name ("english (commentary)") or a number ("english 2").
The manifest is a standard HLS master playlist: the tracks are EXT-X-MEDIA
entries (LANGUAGE is BCP 47, NAME / DEFAULT / FORCED as HLS defines them), the
picture is the EXT-X-STREAM-INF entry, and what HLS has no attribute for goes
where the spec leaves room: EXT-X-SESSION-DATA with reverse-DNS ids
(com.github.yggdrasil75.cim.title / .chapters / .tags / .attachment /
.offset-ms) and X- attributes (X-CODEC, X-TRACK-NAME, X-START-MS, X-TIMESTAMPS,
X-DISPLAY). It keeps the track order, languages, names, flags, audio start
offsets, the title, the display size, and the shift of a file whose audio
started below zero (Matroska written by mkvmerge can't: every track moves later
by the same amount). Players ignore our SESSION-DATA and X- attributes, but it
is still not playable HLS (a raw picture stream isn't a segment): the app
rebuilds a Matroska from the parts to play, download or publish (cached in
<media>/.cim/play).

Other players don't understand this layout: Plex, Jellyfin, Kodi or a file
manager see a raw picture stream without sound and loose audio / subtitle files.
So it is chosen per library (music, tv, movies, other folders - Settings), the
public library folders are never split, and Publish puts a video back together.

The parts move and delete with the picture (media_types.related_exts) and are
never indexed on their own (media_types.is_stream_part). Tracks can be renamed,
relabelled, reordered, removed and added afterwards (manifest edits; added files
are checked with mkvmerge).

mkvtoolnix does the work (mkvextract out, mkvmerge back in). A file is only
split after a rebuild from the parts matches the original: the same time for
every video frame, the same bytes for every non-keyframe packet, the same decoded
picture for every keyframe (a raw stream carries its parameter sets in band),
the same bytes for every audio / subtitle packet with times within 2 ms
(Matroska's millisecond rounding, once each way). Anything else (an unsupported codec, more than one video track, a
mismatch) leaves the file as it was, with the reason. Public movies / tv / music
folders are never split: Plex and Jellyfin read single files.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import uuid

## @brief Matroska codec id -> the raw file mkvextract writes for it.
VIDEO_CODECS = {"V_MPEG4/ISO/AVC": ".avc", "V_MPEGH/ISO/HEVC": ".hevc", "V_VP8": ".ivf",
                "V_VP9": ".ivf", "V_AV1": ".obu", "V_MPEG1": ".m2v", "V_MPEG2": ".m2v"}
AUDIO_CODECS = {"A_AAC": ".aac", "A_AC3": ".ac3", "A_EAC3": ".eac3", "A_DTS": ".dts",
                "A_TRUEHD": ".thd", "A_FLAC": ".flac", "A_MPEG/L3": ".mp3", "A_MPEG/L2": ".mp2",
                "A_OPUS": ".opus", "A_VORBIS": ".ogg", "A_PCM/INT/LIT": ".wav", "A_PCM/INT/BIG": ".wav",
                "A_PCM/FLOAT/IEEE": ".wav", "A_ALAC": ".caf"}
SUB_CODECS = {"S_TEXT/UTF8": ".srt", "S_TEXT/ASCII": ".srt", "S_TEXT/ASS": ".ass", "S_TEXT/SSA": ".ssa",
              "S_TEXT/WEBVTT": ".vtt", "S_HDMV/PGS": ".sup"}
## @brief mkvmerge's codec names (what -J reports for a raw file, which has no codec id).
CODEC_NAMES = {"aac": "A_AAC", "ac-3": "A_AC3", "e-ac-3": "A_EAC3", "dts": "A_DTS", "truehd": "A_TRUEHD",
               "truehd atmos": "A_TRUEHD", "flac": "A_FLAC", "mp3": "A_MPEG/L3", "mp2": "A_MPEG/L2",
               "opus": "A_OPUS", "vorbis": "A_VORBIS", "pcm": "A_PCM/INT/LIT", "alac": "A_ALAC",
               "subrip/srt": "S_TEXT/UTF8", "substationalpha": "S_TEXT/ASS", "webvtt": "S_TEXT/WEBVTT",
               "hdmv pgs": "S_HDMV/PGS"}
## @brief The picture-stream extensions (media_types.STREAM_EXTS).
ES_EXTS = (".avc", ".hevc", ".ivf", ".obu", ".m2v")
MANIFEST_EXT = ".m3u8"
## @brief Our EXT-X-SESSION-DATA ids are DATA_ID + ".streams" / ".title" / ... (reverse DNS,
## as the HLS spec asks); LEGACY_TAG marks the first format, still read.
DATA_ID = "com.github.yggdrasil75.cim"
LEGACY_TAG = "#EXT-X-CIM-STREAMS"
## @brief Public library sections Plex / Jellyfin index: never split.
PUBLIC_SECTIONS = ("movies", "tv", "music")
## @brief Audio / subtitle times may round by this much: Matroska keeps whole milliseconds and
## an audio frame lasts a fraction more (AAC: 23.22 ms), so the original's rounding and a
## rebuild's can differ by one tick each way.
TOLERANCE_MS = 2.0
RUN_TIMEOUT = 6 * 3600
_LANG_CODE = re.compile(r"[A-Za-z]{2,8}(-[A-Za-z0-9]{1,8})*")

## @brief Language code (ISO 639-1, 639-2/B and /T, BCP 47 primary subtag) -> the name its
## files carry. Anything else keeps its code ("Movie - tlh.aac").
LANGUAGES = {
    "en": "english", "eng": "english", "fr": "french", "fre": "french", "fra": "french",
    "de": "german", "ger": "german", "deu": "german", "es": "spanish", "spa": "spanish",
    "it": "italian", "ita": "italian", "pt": "portuguese", "por": "portuguese",
    "nl": "dutch", "dut": "dutch", "nld": "dutch", "sv": "swedish", "swe": "swedish",
    "no": "norwegian", "nor": "norwegian", "nb": "norwegian bokmal", "nob": "norwegian bokmal",
    "nn": "norwegian nynorsk", "nno": "norwegian nynorsk", "da": "danish", "dan": "danish",
    "fi": "finnish", "fin": "finnish", "is": "icelandic", "ice": "icelandic", "isl": "icelandic",
    "pl": "polish", "pol": "polish", "cs": "czech", "cze": "czech", "ces": "czech",
    "sk": "slovak", "slo": "slovak", "slk": "slovak", "sl": "slovenian", "slv": "slovenian",
    "hu": "hungarian", "hun": "hungarian", "ro": "romanian", "rum": "romanian", "ron": "romanian",
    "bg": "bulgarian", "bul": "bulgarian", "hr": "croatian", "hrv": "croatian",
    "sr": "serbian", "srp": "serbian", "bs": "bosnian", "bos": "bosnian",
    "uk": "ukrainian", "ukr": "ukrainian", "ru": "russian", "rus": "russian",
    "be": "belarusian", "bel": "belarusian", "el": "greek", "gre": "greek", "ell": "greek",
    "tr": "turkish", "tur": "turkish", "he": "hebrew", "heb": "hebrew", "ar": "arabic", "ara": "arabic",
    "fa": "persian", "per": "persian", "fas": "persian", "hi": "hindi", "hin": "hindi",
    "bn": "bengali", "ben": "bengali", "ta": "tamil", "tam": "tamil", "te": "telugu", "tel": "telugu",
    "ur": "urdu", "urd": "urdu", "th": "thai", "tha": "thai", "vi": "vietnamese", "vie": "vietnamese",
    "id": "indonesian", "ind": "indonesian", "ms": "malay", "may": "malay", "msa": "malay",
    "tl": "tagalog", "tgl": "tagalog", "fil": "filipino", "zh": "chinese", "chi": "chinese",
    "zho": "chinese", "cmn": "mandarin", "yue": "cantonese", "ja": "japanese", "jpn": "japanese",
    "ko": "korean", "kor": "korean", "ca": "catalan", "cat": "catalan", "eu": "basque",
    "baq": "basque", "eus": "basque", "gl": "galician", "glg": "galician", "ga": "irish",
    "gle": "irish", "cy": "welsh", "wel": "welsh", "cym": "welsh", "et": "estonian", "est": "estonian",
    "lv": "latvian", "lav": "latvian", "lt": "lithuanian", "lit": "lithuanian",
    "la": "latin", "lat": "latin", "sw": "swahili", "swa": "swahili", "af": "afrikaans",
    "afr": "afrikaans", "zxx": "no linguistic content", "mul": "multiple", "und": "unknown",
}
## @brief Regional variants kept distinct from their language.
REGIONS = {"pt-br": "brazilian portuguese", "es-419": "latin american spanish",
           "es-mx": "mexican spanish", "fr-ca": "canadian french", "zh-hans": "chinese (simplified)",
           "zh-hant": "chinese (traditional)", "zh-tw": "chinese (taiwan)", "zh-hk": "chinese (hong kong)",
           "en-gb": "british english", "en-us": "american english"}


class DemuxError(Exception):
    """! @brief This can't be done (the message says why); nothing was changed."""


def available():
    """! @brief True when mkvmerge, mkvextract, ffmpeg and ffprobe are on PATH."""
    return all(shutil.which(t) for t in ("mkvmerge", "mkvextract", "ffmpeg", "ffprobe"))


def manifest_path(abs_path):
    """! @brief Where an item's manifest is (beside it, same stem)."""
    return os.path.splitext(abs_path)[0] + MANIFEST_EXT


def is_demuxed(abs_path):
    """! @brief True for a library item stored as separate streams."""
    return os.path.splitext(abs_path)[1].lower() in ES_EXTS and read_manifest(abs_path) is not None


def _run(cmd, timeout=RUN_TIMEOUT):
    """! @brief Run a tool. @return (ok, stdout, stderr). mkvtoolnix exits 1 on warnings only."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, "", str(e)
    ok = p.returncode == 0 or (cmd[0] in ("mkvmerge", "mkvextract") and p.returncode == 1)
    return ok, p.stdout, p.stderr or p.stdout


# -- languages and names --------------------------------------------------------------------
def language_name(code):
    """! @brief "en" / "eng" -> "english"; "pt-BR" -> "brazilian portuguese"; unknown codes as given."""
    c = str(code or "und").strip().lower().replace("_", "-")
    if c in REGIONS:
        return REGIONS[c]
    return LANGUAGES.get(c) or LANGUAGES.get(c.split("-")[0]) or c


def valid_language(code):
    """! @brief True for a BCP 47 / ISO 639 looking code (en, eng, pt-BR, zh-Hant)."""
    return bool(_LANG_CODE.fullmatch(str(code or "")))


def _clean(s):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(s or "")).strip().rstrip(". ")
    return re.sub(r"\s+", " ", s)[:60]


def part_label(track):
    """! @brief What a track's file is called after "Movie - ": its language, plus its name or
    "forced" when it has one ("english (commentary)", "english (forced)")."""
    label = language_name(track.get("language"))
    extra = _clean(track.get("name"))
    if extra and extra.lower() != label:
        label += f" ({extra})"
    elif track.get("forced"):
        label += " (forced)"
    return label


def unique_name(stem, label, ext, taken):
    """! @brief "stem - label.ext", numbered ("label 2") while taken (a set of lower-case names,
    which gets the chosen name)."""
    n = 1
    while True:
        name = f"{stem} - {label}{'' if n == 1 else ' %d' % n}{ext}"
        if name.lower() not in taken:
            taken.add(name.lower())
            return name
        n += 1


# -- the manifest (HLS syntax) ------------------------------------------------------------
def _q(v):
    return '"' + str(v).replace('"', "'").replace("\n", " ") + '"'


def _yes(v):
    return "YES" if v else "NO"


def _data(name, value, **extra):
    """! @brief One EXT-X-SESSION-DATA line of ours (DATA-ID in our reverse-DNS space,
    client-defined X- attributes for the rest)."""
    out = f"#EXT-X-SESSION-DATA:DATA-ID={_q(DATA_ID + '.' + name)},VALUE={_q(value)}"
    for k, v in extra.items():
        out += f",X-{k.upper().replace('_', '-')}={_q(v)}"
    return out


def write_manifest(path, m):
    """! @brief Write a manifest {title, video, tracks, chapters, tags, attachments, offset_ms}
    as an HLS master playlist. Only standard tags: EXT-X-MEDIA for the tracks (TYPE,
    GROUP-ID, NAME, LANGUAGE, DEFAULT, AUTOSELECT, FORCED, URI), EXT-X-STREAM-INF + URI
    line for the picture (BANDWIDTH, RESOLUTION, AUDIO, SUBTITLES), EXT-X-SESSION-DATA
    (DATA-ID com.github.yggdrasil75.cim.*) for the rest, and X- attributes (RFC 8216
    reserves the X- prefix for client-defined attributes) for what HLS has no attribute
    for. Players ignore what they don't know."""
    v = m["video"]
    lines = ["#EXTM3U", "#EXT-X-VERSION:7", _data("streams", "1")]
    if m.get("title"):
        lines.append(_data("title", m["title"]))
    if m.get("offset_ms"):
        lines.append(_data("offset-ms", "%d" % round(m["offset_ms"])))
    if m.get("chapters"):
        lines.append(_data("chapters", m["chapters"], uri=m["chapters"]))
    if m.get("tags"):
        lines.append(_data("tags", m["tags"], uri=m["tags"]))
    for a in m.get("attachments") or []:
        lines.append(_data("attachment", a["name"], mime=a.get("mime") or "application/octet-stream",
                           description=a.get("description") or "", uri=a["file"]))
    groups = {"audio": ("AUDIO", "audio"), "subtitles": ("SUBTITLES", "subs")}
    for t in m.get("tracks") or []:
        typ, gid = groups[t["type"]]
        attrs = [f"TYPE={typ}", f"GROUP-ID={_q(gid)}", f"NAME={_q(part_label(t).title())}",
                 f"LANGUAGE={_q(t.get('language') or 'und')}", f"DEFAULT={_yes(t.get('default'))}",
                 f"AUTOSELECT={_yes(t.get('default'))}"]
        if t["type"] == "subtitles":
            attrs.append(f"FORCED={_yes(t.get('forced'))}")
        attrs.append(f"X-CODEC={_q(t.get('codec') or '')}")
        if t.get("name"):
            attrs.append(f"X-TRACK-NAME={_q(t['name'])}")
        if t["type"] == "audio" and t.get("start_ms"):
            attrs.append("X-START-MS=%d" % round(t["start_ms"]))
        attrs.append(f"URI={_q(t['file'])}")
        lines.append("#EXT-X-MEDIA:" + ",".join(attrs))
    has = {t["type"] for t in m.get("tracks") or []}
    inf = ["BANDWIDTH=%d" % max(1, int(v.get("bandwidth") or 1))]
    if v.get("pixel_dimensions"):
        inf.append("RESOLUTION=" + v["pixel_dimensions"])
    if "audio" in has:
        inf.append('AUDIO="audio"')
    if "subtitles" in has:
        inf.append('SUBTITLES="subs"')
    inf += [f"X-CODEC={_q(v.get('codec') or '')}", f"X-LANGUAGE={_q(v.get('language') or 'und')}",
            f"X-DEFAULT={_yes(v.get('default', True))}", f"X-TIMESTAMPS={_q(v['timestamps'])}"]
    if v.get("name"):
        inf.append(f"X-TRACK-NAME={_q(v['name'])}")
    if v.get("display_dimensions") and v["display_dimensions"] != v.get("pixel_dimensions"):
        inf.append("X-DISPLAY=" + v["display_dimensions"])
    lines += ["#EXT-X-STREAM-INF:" + ",".join(inf), v["file"], ""]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    os.replace(tmp, path)


_ATTR = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def _attrs(s):
    """! @brief An attribute list -> {NAME: value}; the early CIM- attribute names read as X-."""
    out = {}
    for k, v in _ATTR.findall(s):
        out["X-" + k[4:] if k.startswith("CIM-") else k] = v[1:-1] if v.startswith('"') else v
    return out


def is_manifest_text(text):
    """! @brief True for our manifest (the streams SESSION-DATA, or the early #EXT-X-CIM-STREAMS)."""
    return bool(text) and (f'DATA-ID="{DATA_ID}.streams"' in text or LEGACY_TAG in text)


def parse_manifest(text):
    """! @brief A manifest's text -> {title, video, tracks, chapters, tags, attachments,
    offset_ms}, or None when it isn't one of ours."""
    if not is_manifest_text(text):
        return None
    m = {"title": "", "video": None, "tracks": [], "chapters": None, "tags": None, "attachments": [],
         "offset_ms": 0.0}
    pending = None
    legacy = {"#EXT-X-CIM-TITLE": "title", "#EXT-X-CIM-OFFSET-MS": "offset-ms",
              "#EXT-X-CIM-CHAPTERS": "chapters", "#EXT-X-CIM-TAGS": "tags",
              "#EXT-X-CIM-ATTACHMENT": "attachment"}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        head, _, rest = line.partition(":")
        if head in legacy:                     # the first format, before SESSION-DATA
            a = _attrs(rest)
            name = legacy[head]
            value = a.get("NAME") if name == "attachment" else (
                a.get("URI") if name in ("chapters", "tags") else (rest[1:-1] if rest.startswith('"') else rest))
            a = {"DATA-ID": f"{DATA_ID}.{name}", "VALUE": value or "", "X-URI": a.get("URI", ""),
                 "X-MIME": a.get("MIME", ""), "X-DESCRIPTION": a.get("DESCRIPTION", "")}
            head = "#EXT-X-SESSION-DATA"
        else:
            a = _attrs(rest)
        if head == "#EXT-X-SESSION-DATA":
            did = a.get("DATA-ID", "")
            if not did.startswith(DATA_ID + "."):
                continue
            name, value = did[len(DATA_ID) + 1:], a.get("VALUE", "")
            if name == "title":
                m["title"] = value
            elif name == "offset-ms":
                try:
                    m["offset_ms"] = float(value)
                except ValueError:
                    pass
            elif name in ("chapters", "tags"):
                m[name] = a.get("X-URI") or value or None
            elif name == "attachment":
                m["attachments"].append({"name": value, "mime": a.get("X-MIME", ""),
                                         "description": a.get("X-DESCRIPTION", ""), "file": a.get("X-URI", "")})
        elif head == "#EXT-X-MEDIA":
            typ = {"AUDIO": "audio", "SUBTITLES": "subtitles"}.get(a.get("TYPE"))
            if typ and a.get("URI"):
                try:
                    start = float(a.get("X-START-MS") or 0)
                except ValueError:
                    start = 0.0
                m["tracks"].append({"type": typ, "file": a["URI"], "codec": a.get("X-CODEC", ""),
                                    "language": a.get("LANGUAGE", "und"), "name": a.get("X-TRACK-NAME", ""),
                                    "default": a.get("DEFAULT") == "YES", "forced": a.get("FORCED") == "YES",
                                    "start_ms": start})
        elif head == "#EXT-X-STREAM-INF":
            pending = a
        elif not line.startswith("#") and pending is not None and m["video"] is None:
            a = pending
            try:
                bw = int(a.get("BANDWIDTH") or 1)
            except ValueError:
                bw = 1
            m["video"] = {"file": line, "codec": a.get("X-CODEC", ""), "language": a.get("X-LANGUAGE", "und"),
                          "name": a.get("X-TRACK-NAME", ""), "default": a.get("X-DEFAULT", "YES") == "YES",
                          "timestamps": a.get("X-TIMESTAMPS", ""), "pixel_dimensions": a.get("RESOLUTION", ""),
                          "display_dimensions": a.get("X-DISPLAY") or a.get("RESOLUTION", ""), "bandwidth": bw}
            pending = None
    return m if m["video"] and m["video"].get("timestamps") else None


def read_manifest(item_abs):
    """! @brief The manifest beside an item, or None."""
    try:
        with open(manifest_path(item_abs), encoding="utf-8") as f:
            return parse_manifest(f.read())
    except (OSError, UnicodeDecodeError):
        return None


def part_files(m):
    """! @brief Every file name a manifest lists except the picture stream."""
    names = [m["video"]["timestamps"], m.get("chapters"), m.get("tags")]
    names += [t["file"] for t in m.get("tracks") or []] + [a["file"] for a in m.get("attachments") or []]
    return [n for n in names if n]


# -- identification and comparison -----------------------------------------------------------
def identify(path):
    """! @brief mkvmerge's identification (JSON) of a file. @throws DemuxError."""
    ok, out, err = _run(["mkvmerge", "-J", path], timeout=600)
    try:
        d = json.loads(out or "{}")
    except ValueError:
        d = {}
    if not ok or not d.get("container", {}).get("recognized", True) or not d.get("tracks"):
        raise DemuxError("mkvmerge can't read it: " + (err or "no tracks").strip()[-200:])
    return d


def _track_meta(t):
    p = t.get("properties") or {}
    return {"language": p.get("language_ietf") or p.get("language") or "und", "name": p.get("track_name") or "",
            "default": bool(p.get("default_track")), "forced": bool(p.get("forced_track")),
            "start_ms": (p.get("minimum_timestamp") or 0) / 1e6}


def _duration(d):
    try:
        return float((d.get("container", {}).get("properties") or {}).get("duration") or 0) / 1e9
    except (TypeError, ValueError):
        return 0.0


def packets(path):
    """! @brief Every packet of a file: {stream_index: [(pts, keyframe, md5)]} in file order.
    @throws DemuxError."""
    ok, out, err = _run(["ffprobe", "-v", "error", "-show_packets", "-show_data_hash", "md5",
                         "-show_entries", "packet=stream_index,pts,flags,data_hash", "-of", "csv=p=0", path])
    if not ok:
        raise DemuxError("ffprobe: " + (err or "").strip()[-200:])
    res = {}
    for line in out.splitlines():
        parts = line.split(",")
        digest = next((x for x in parts[3:] if x.startswith("MD5:")), None)
        if len(parts) < 4 or digest is None:
            continue                         # a side-data line, not a packet
        try:
            idx = int(parts[0])
            pts = int(parts[1]) if parts[1] not in ("", "N/A") else None
        except ValueError:
            continue
        res.setdefault(idx, []).append((pts, parts[2].startswith("K"), digest))
    return res


def stream_kinds(path):
    """! @brief [(index, codec_type, time_base seconds)] of a file's streams."""
    ok, out, _err = _run(["ffprobe", "-v", "error", "-show_entries", "stream=index,codec_type,time_base",
                          "-of", "json", path], timeout=120)
    out = json.loads(out or "{}") if ok else {}
    res = []
    for s in out.get("streams") or []:
        try:
            n, d = (s.get("time_base") or "1/1000").split("/")
            tb = int(n) / int(d)
        except (ValueError, ZeroDivisionError):
            tb = 0.001
        res.append((s["index"], s.get("codec_type"), tb))
    return res


def keyframe_pictures(path, stream):
    """! @brief MD5 of every decoded keyframe of one video stream, in order."""
    ok, out, err = _run(["ffmpeg", "-v", "error", "-skip_frame", "nokey", "-i", path, "-map", f"0:{stream}",
                         "-an", "-sn", "-fps_mode", "passthrough", "-f", "framemd5", "-"])
    if not ok:
        raise DemuxError("ffmpeg keyframe check: " + (err or "").strip()[-200:])
    return [ln.rsplit(",", 1)[-1].strip() for ln in out.splitlines() if ln and not ln.startswith("#")]


def start_times(path):
    """! @brief {stream index: first time in ms} (ffprobe's start_time; AAC priming makes
    an audio start below zero)."""
    ok, out, _err = _run(["ffprobe", "-v", "error", "-show_entries", "stream=index,start_time",
                          "-of", "json", path], timeout=120)
    res = {}
    for st in (json.loads(out or "{}") if ok else {}).get("streams") or []:
        try:
            res[st["index"]] = float(st.get("start_time")) * 1000
        except (TypeError, ValueError):
            pass
    return res


def compare(original, rebuilt, offset_ms=0.0):
    """! @brief None when `rebuilt` holds the same streams as `original` (every time later by
    `offset_ms`), else the difference."""
    ka, kb = stream_kinds(original), stream_kinds(rebuilt)
    if [k for _, k, _ in ka] != [k for _, k, _ in kb]:
        return "the streams differ: %s vs %s" % ([k for _, k, _ in ka], [k for _, k, _ in kb])
    pa, pb = packets(original), packets(rebuilt)
    for (ia, kind, tba), (ib, _k, tbb) in zip(ka, kb):
        if kind == "attachment":
            continue
        a, b = pa.get(ia, []), pb.get(ib, [])
        if len(a) != len(b):
            return f"stream {ia} ({kind}): {len(a)} packets, rebuilt {len(b)}"
        limit = (0.5 if kind == "video" else TOLERANCE_MS) + 1e-6
        for n, ((ta, keya, ha), (tb_, _keyb, hb)) in enumerate(zip(a, b)):
            ms_a = None if ta is None else ta * tba * 1000
            ms_b = None if tb_ is None else tb_ * tbb * 1000 - offset_ms
            if (ms_a is None) != (ms_b is None) or (ms_a is not None and abs(ms_a - ms_b) > limit):
                return f"stream {ia} ({kind}) packet {n}: time {ms_a} ms, rebuilt {ms_b} ms"
            if kind == "video" and keya:
                continue                    # parameter sets in band: compared decoded, below
            if ha != hb:
                return f"stream {ia} ({kind}) packet {n}: the bytes differ"
        if kind == "video" and keyframe_pictures(original, ia) != keyframe_pictures(rebuilt, ib):
            return f"stream {ia}: a keyframe decodes differently"
    return None


# -- splitting and rebuilding -------------------------------------------------------------
def _flags(meta, video=False):
    """! @brief mkvmerge options for track 0 of the next input from a manifest entry."""
    o = ["--language", "0:" + (meta.get("language") or "und"),
         "--default-track-flag", "0:" + ("1" if meta.get("default", video) else "0")]
    if not video:
        o += ["--forced-display-flag", "0:" + ("1" if meta.get("forced") else "0")]
    return o + ["--track-name", "0:" + (meta.get("name") or "")]


def mux_files(m, path_of, out):
    """! @brief Build one Matroska from a manifest whose files `path_of(name)` finds.
    @return None or the error."""
    v = m["video"]
    inputs = [path_of(v["timestamps"]), path_of(v["file"])] + [path_of(t["file"]) for t in m.get("tracks") or []]
    inputs += [path_of(x) for x in (m.get("chapters"), m.get("tags")) if x]
    inputs += [path_of(a["file"]) for a in m.get("attachments") or []]
    missing = [p for p in inputs if not os.path.exists(p)]
    if missing:
        return "missing: " + ", ".join(os.path.basename(p) for p in missing[:3])
    cmd = ["mkvmerge", "-q", "-o", out, "--title", m.get("title") or ""]
    if m.get("chapters"):
        cmd += ["--chapters", path_of(m["chapters"])]
    if m.get("tags"):
        cmd += ["--global-tags", path_of(m["tags"])]
    cmd += _flags(v, video=True) + ["--timestamps", "0:" + path_of(v["timestamps"])]
    if v.get("display_dimensions") and v["display_dimensions"] != v.get("pixel_dimensions"):
        cmd += ["--display-dimensions", "0:" + v["display_dimensions"]]
    cmd.append(path_of(v["file"]))
    for t in m.get("tracks") or []:
        cmd += _flags(t)
        if t["type"] == "audio" and t.get("start_ms"):
            # raw audio starts at 0: its first time
            cmd += ["--sync", "0:%d" % round(t["start_ms"])]
        elif t["type"] == "subtitles" and m.get("offset_ms"):
            # text / PGS subtitles carry their own times: only the whole file's shift
            cmd += ["--sync", "0:%d" % round(m["offset_ms"])]
        cmd.append(path_of(t["file"]))
    for a in m.get("attachments") or []:
        cmd += ["--attachment-name", a["name"] or "attachment",
                "--attachment-mime-type", a.get("mime") or "application/octet-stream"]
        if a.get("description"):
            cmd += ["--attachment-description", a["description"]]
        cmd += ["--attach-file", path_of(a["file"])]
    ok, _o, err = _run(cmd)
    return None if ok else (err or "mkvmerge failed").strip()[-300:]


def split(src, stem, taken, work_dir):
    """! @brief Split a video into its parts and check that they rebuild it.
    @param stem   the item's file stem ("Movie"): the parts are named after it.
    @param taken  file names already in the item's folder (never reused).
    @return (manifest, {final name: temp path}) - the picture is manifest["video"]["file"].
    @throws DemuxError with the reason the file is left alone.
    """
    os.makedirs(work_dir, exist_ok=True)
    tag = uuid.uuid4().hex
    mkv, created, keep = src, [], set()
    try:
        if os.path.splitext(src)[1].lower() not in (".mkv", ".webm"):
            mkv = os.path.join(work_dir, tag + "-src.mkv")       # lossless rewrap first
            created.append(mkv)
            ok, _o, err = _run(["mkvmerge", "-q", "-o", mkv, src])
            if not ok:
                raise DemuxError("mkvmerge can't rewrap it: " + err.strip()[-200:])
        d = identify(mkv)
        videos = [t for t in d["tracks"] if t["type"] == "video"]
        if len(videos) != 1:
            raise DemuxError(f"{len(videos)} video tracks (only single-video files are split)")
        taken = {n.lower() for n in taken}
        existing = set(taken)
        # Matroska from mkvmerge can't start below zero; a file that does (AAC priming)
        # is shifted as a whole, every track by the same amount
        starts = start_times(mkv)
        shift = max(0.0, -min(starts.values(), default=0.0))
        vmeta = videos[0].get("properties") or {}
        vext = VIDEO_CODECS.get(vmeta.get("codec_id"))
        if not vext:
            raise DemuxError(f"the video is {vmeta.get('codec_id') or videos[0].get('codec')}, "
                             "which has no raw form here")
        m = {"title": (d.get("container", {}).get("properties") or {}).get("title") or "",
             "video": {**_track_meta(videos[0]), "codec": vmeta.get("codec_id"), "file": stem + vext,
                       "timestamps": stem + ".timestamps.txt",
                       "pixel_dimensions": vmeta.get("pixel_dimensions") or "",
                       "display_dimensions": vmeta.get("display_dimensions") or "",
                       "bandwidth": int(os.path.getsize(src) * 8 / max(1.0, _duration(d)))},
             "tracks": [], "chapters": None, "tags": None, "attachments": [], "offset_ms": shift}
        files = {}                                       # final name -> temp path

        def tmp(name):
            p = os.path.join(work_dir, f"{tag}-{len(files)}{os.path.splitext(name)[1]}")
            files[name] = p
            created.append(p)
            return p

        cmd = ["mkvextract", mkv, "tracks", f"{videos[0]['id']}:{tmp(m['video']['file'])}"]
        for t in d["tracks"]:
            if t["type"] == "video":
                continue
            cid = (t.get("properties") or {}).get("codec_id") or ""
            table = {"audio": AUDIO_CODECS, "subtitles": SUB_CODECS}.get(t["type"], {})
            if cid not in table:
                raise DemuxError(f"{t['type']} track {t['id']} is {cid or t.get('codec')}, which has no raw form here")
            meta = _track_meta(t)
            if t["type"] == "audio":
                meta["start_ms"] = max(0.0, starts.get(t["id"], meta["start_ms"]) + shift)
            name = unique_name(stem, part_label(meta), table[cid], taken)
            m["tracks"].append({"type": t["type"], "codec": cid, "file": name, **meta})
            cmd.append(f"{t['id']}:{tmp(name)}")
        cmd += ["timestamps_v2", f"{videos[0]['id']}:{tmp(m['video']['timestamps'])}"]
        if d.get("chapters"):
            m["chapters"] = stem + ".chapters.xml"
            cmd += ["chapters", tmp(m["chapters"])]
        if any((g or {}).get("num_entries") for g in d.get("global_tags") or []):
            m["tags"] = stem + ".tags.xml"
            cmd += ["tags", tmp(m["tags"])]
        atts = []
        for a in d.get("attachments") or []:
            fname = a.get("file_name") or "attachment"
            name = unique_name(stem, "attachment - " + (_clean(os.path.splitext(fname)[0]) or "file"),
                               os.path.splitext(fname)[1].lower(), taken)
            m["attachments"].append({"name": fname, "mime": a.get("content_type") or "application/octet-stream",
                                     "description": a.get("description") or "", "file": name})
            atts.append(f"{a['id']}:{tmp(name)}")
        if atts:
            cmd += ["attachments"] + atts
        clash = [n for n in files if n.lower() in existing and n != m["video"]["file"]]
        if clash:
            raise DemuxError(f"{clash[0]} exists already")
        ok, _o, err = _run(cmd)
        if not ok:
            raise DemuxError("mkvextract: " + err.strip()[-200:])
        if shift:
            _shift_timestamps(files[m["video"]["timestamps"]], shift)
        rebuilt = os.path.join(work_dir, tag + "-rebuilt.mkv")
        created.append(rebuilt)
        err = mux_files(m, lambda n: files[n], rebuilt)
        if err:
            raise DemuxError("rebuild failed: " + err)
        diff = compare(mkv, rebuilt, shift)
        if diff:
            raise DemuxError("the parts don't rebuild it exactly (" + diff + ")")
        keep = set(files.values())
        return m, files
    finally:
        for p in created:                    # never the source: only what was made here
            if p not in keep and os.path.exists(p):
                os.remove(p)


def _shift_timestamps(path, ms):
    """! @brief Add `ms` to every time of a "timestamp v2" file."""
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    out = []
    for ln in lines:
        try:
            out.append(("%.6f" % (float(ln) + ms)).rstrip("0").rstrip("."))
        except ValueError:
            out.append(ln)                   # the header
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")


def mux(item_abs, out):
    """! @brief Rebuild a split item as one Matroska at `out`. @return None or the error."""
    m = read_manifest(item_abs)
    if m is None:
        return "no manifest"
    d = os.path.dirname(item_abs)
    tmp = out + ".part.mkv"
    try:
        err = mux_files(m, lambda n: item_abs if n == m["video"]["file"] else os.path.join(d, n), tmp)
        if err is None:
            os.replace(tmp, out)
        return err
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


class Demuxer:
    """! @brief Split / rebuild library items in place (through the core's move_file), track
    edits, the rebuilt copies playback and downloads use, and uploads queued for splitting."""

    def __init__(self, host, scopes, after=None):
        """! @param scopes   fn() -> the libraries the setting splits ("music", "tv", "movies",
                        "other").
        @param after    fn(rel, abs) once an upload was handled (queue its browser copy)."""
        self.host = host
        self.scopes = scopes
        self.after = after or (lambda rel, ap: None)
        cim = os.path.abspath(os.path.join(host.media_dir, ".cim"))
        self.work = os.path.join(cim, "demux_tmp")
        self.play = os.path.join(cim, "play")
        self.lock = threading.Lock()
        self.queue = []
        self.wake = threading.Event()
        self.worker = None
        self.last_error = {}

    # -- helpers -----------------------------------------------------------------------
    def public(self, rel):
        """! @brief True for a file in a public movies / tv / music folder (kept whole)."""
        sid, user, _inner = self.host.core.library_section(rel)
        return sid in PUBLIC_SECTIONS and not user

    def scope(self, rel):
        """! @brief Which library a file is in for the setting: "music" / "tv" / "movies" (public
        or private), else "other"."""
        sid, _user, _inner = self.host.core.library_section(rel)
        return sid if sid in PUBLIC_SECTIONS else "other"

    def in_scope(self, rel):
        """! @brief True when the setting splits this file's library (never a public folder)."""
        return not self.public(rel) and self.scope(rel) in (self.scopes() or [])

    def _abs(self, rel):
        ap = self.host.safe_path(self.host.media_dir, rel)
        if not ap or not os.path.isfile(ap):
            raise DemuxError("file missing")
        return ap

    def _split_item(self, rel):
        """! @brief (abs path, manifest) of a split item. @throws DemuxError."""
        ap = self._abs(rel)
        m = read_manifest(ap) if os.path.splitext(ap)[1].lower() in ES_EXTS else None
        if m is None:
            raise DemuxError("not stored as separate streams")
        return ap, m

    def _key(self, item_abs):
        rel = os.path.relpath(item_abs, self.host.media_dir).replace(os.sep, "/")
        return "demux-" + hashlib.sha1(rel.encode("utf-8")).hexdigest()[:20]

    def drop(self, rel):
        """! @brief Remove the cached rebuilds of an item (deleted / renamed / tracks changed)."""
        if not os.path.isdir(self.play):
            return
        key = self._key(os.path.join(self.host.media_dir, rel)) + "-"
        for name in os.listdir(self.play):
            if name.startswith(key):
                try:
                    os.remove(os.path.join(self.play, name))
                except OSError:
                    pass

    def rebuilt(self, item_abs):
        """! @brief The cached Matroska rebuilt from a split item (made when missing).
        @return its path, or None when it can't be built."""
        m = read_manifest(item_abs)
        if m is None:
            return None
        d = os.path.dirname(item_abs)
        stamps = [manifest_path(item_abs), item_abs] + [os.path.join(d, n) for n in part_files(m)]
        mt = int(max((os.path.getmtime(p) for p in stamps if os.path.exists(p)), default=0))
        key = self._key(item_abs)
        out = os.path.join(self.play, "%s-%d.mkv" % (key, mt))
        if os.path.exists(out):
            return out
        os.makedirs(self.play, exist_ok=True)
        with self.lock:
            if os.path.exists(out):
                return out
            err = mux(item_abs, out)
        if err:
            self.host.logger.warning(f"separate streams: can't rebuild {item_abs}: {err}")
            return None
        for name in os.listdir(self.play):       # older rebuilds of this item
            if name.startswith(key + "-") and name != os.path.basename(out):
                try:
                    os.remove(os.path.join(self.play, name))
                except OSError:
                    pass
        return out

    def source(self, abs_path):
        """! @brief What to read for a video: its rebuild when split, else itself."""
        if os.path.splitext(abs_path)[1].lower() in ES_EXTS and is_demuxed(abs_path):
            return self.rebuilt(abs_path) or abs_path
        return abs_path

    # -- split / put back in place ---------------------------------------------------------
    def demux_rel(self, rel, keep_original=False):
        """! @brief Store one library video as separate streams.
        @return the new rel_path. @throws DemuxError (the file is left as it was)."""
        host = self.host
        ap = self._abs(rel)
        if os.path.splitext(ap)[1].lower() in ES_EXTS:
            raise DemuxError("already separate streams")
        if self.public(rel):
            raise DemuxError("public library folder: Plex / Jellyfin need single files")
        if not available():
            raise DemuxError("needs mkvtoolnix (mkvmerge, mkvextract)")
        d = os.path.dirname(ap)
        stem = os.path.splitext(os.path.basename(ap))[0]
        if os.path.exists(os.path.join(d, stem + MANIFEST_EXT)):
            raise DemuxError(f"{stem}{MANIFEST_EXT} exists already")
        m, files = split(ap, stem, set(os.listdir(d)), self.work)
        placed = []
        try:
            picture = files.pop(m["video"]["file"])
            for name, p in files.items():          # the parts first, then the manifest,
                dst = os.path.join(d, name)        # then the picture replaces the file
                shutil.move(p, dst)
                placed.append(dst)
            if keep_original:
                bak = os.path.join(host.media_dir, ".cim", "reencoded", rel)
                os.makedirs(os.path.dirname(bak), exist_ok=True)
                shutil.copy2(ap, bak)
            write_manifest(os.path.join(d, stem + MANIFEST_EXT), m)
            placed.append(os.path.join(d, stem + MANIFEST_EXT))
            new_rel = os.path.splitext(rel)[0] + os.path.splitext(m["video"]["file"])[1]
            ok, err = host.core.move_file(rel, new_rel, content=picture)
            if not ok:
                raise DemuxError(err or "replace failed")
            placed = []
            return new_rel
        finally:
            for p in placed:                       # failed: the folder as it was
                if os.path.exists(p):
                    os.remove(p)
            for p in files.values():
                if os.path.exists(p):
                    os.remove(p)

    def mux_rel(self, rel):
        """! @brief Put a split item back together as one .mkv in place.
        @return the new rel_path. @throws DemuxError."""
        ap, m = self._split_item(rel)
        os.makedirs(self.work, exist_ok=True)
        out = os.path.join(self.work, uuid.uuid4().hex + ".mkv")
        err = mux(ap, out)
        if err:
            raise DemuxError(err)
        d = os.path.dirname(ap)
        parts = [os.path.join(d, n) for n in part_files(m)] + [manifest_path(ap)]
        new_rel = os.path.splitext(rel)[0] + ".mkv"
        ok, err = self.host.core.move_file(rel, new_rel, content=out)
        if os.path.exists(out):
            os.remove(out)
        if not ok:
            raise DemuxError(err or "replace failed")
        for p in parts:                            # in the .mkv now
            if os.path.exists(p):
                os.remove(p)
        self.drop(rel)
        return new_rel

    def renamed(self, old_rel, new_rel):
        """! @brief After the core moved a split item (its parts went along, renamed from the
        old stem to the new one): point the manifest at the new names."""
        ap = self.host.safe_path(self.host.media_dir, new_rel)
        if not ap or os.path.splitext(ap)[1].lower() not in ES_EXTS:
            return
        old = os.path.splitext(os.path.basename(old_rel))[0]
        new = os.path.splitext(os.path.basename(new_rel))[0]
        m = read_manifest(ap)
        if m is None or old == new:
            return
        fix = lambda n: (new + n[len(old):]) if n and n.startswith(old) else n
        m["video"]["file"] = fix(m["video"]["file"])
        m["video"]["timestamps"] = fix(m["video"]["timestamps"])
        m["chapters"], m["tags"] = fix(m.get("chapters")), fix(m.get("tags"))
        for t in m["tracks"] + m["attachments"]:
            t["file"] = fix(t["file"])
        write_manifest(manifest_path(ap), m)

    # -- tracks ------------------------------------------------------------------------------
    def tracks(self, rel):
        """! @brief A split item's tracks for the editor. @throws DemuxError."""
        ap, m = self._split_item(rel)
        d = os.path.dirname(ap)
        size = lambda n: os.path.getsize(os.path.join(d, n)) if os.path.exists(os.path.join(d, n)) else None
        return {"title": m["title"], "video": {**m["video"], "size": os.path.getsize(ap)},
                "tracks": [{**t, "size": size(t["file"]), "label": part_label(t)} for t in m["tracks"]],
                "attachments": [{**a, "size": size(a["file"])} for a in m["attachments"]],
                "chapters": bool(m.get("chapters"))}

    def set_tracks(self, rel, tracks, title=None):
        """! @brief Reorder, relabel and remove tracks: `tracks` is the new list
        [{file, language, name, default, forced, start_ms?}] (by current file name). A track
        left out is deleted; a file is renamed when its language / name changes its label.
        @return tracks(rel). @throws DemuxError (nothing changed)."""
        ap, m = self._split_item(rel)
        d = os.path.dirname(ap)
        stem = os.path.splitext(os.path.basename(ap))[0]
        by_file = {t["file"]: t for t in m["tracks"]}
        new, seen = [], set()
        for t in tracks or []:
            f = str((t or {}).get("file") or "")
            if f not in by_file or f in seen:
                raise DemuxError(f"no track {f!r}")
            seen.add(f)
            old = by_file[f]
            lang = str(t.get("language") or old["language"] or "und").strip()
            if not valid_language(lang):
                raise DemuxError(f"{lang!r} is not a language code (en, fr, pt-BR, ...)")
            try:
                start = float(t.get("start_ms", old.get("start_ms") or 0) or 0)
            except (TypeError, ValueError):
                raise DemuxError("start offset must be a number of milliseconds")
            new.append({**old, "language": lang, "name": _clean(t.get("name", old["name"])),
                        "default": bool(t.get("default")), "forced": bool(t.get("forced")), "start_ms": start})
        gone = [f for f in by_file if f not in seen]
        # names: each kept track by its (maybe new) language / name, never another file's
        taken = {n.lower() for n in os.listdir(d)} - {f.lower() for f in by_file}
        renames = []
        for t in new:
            want = unique_name(stem, part_label(t), os.path.splitext(t["file"])[1], taken)
            if want != t["file"]:
                renames.append((t["file"], want))
                t["file"] = want
        staged = []                                # two steps, so swapped names can't collide
        for old, want in renames:
            tmpn = f".cim-{uuid.uuid4().hex}{os.path.splitext(old)[1]}"
            os.replace(os.path.join(d, old), os.path.join(d, tmpn))
            staged.append((tmpn, want))
        for tmpn, want in staged:
            os.replace(os.path.join(d, tmpn), os.path.join(d, want))
        for f in gone:
            p = os.path.join(d, f)
            if os.path.exists(p):
                os.remove(p)
        m["tracks"] = new
        if title is not None:
            m["title"] = str(title)[:300]
        write_manifest(manifest_path(ap), m)
        self.drop(rel)
        return self.tracks(rel)

    def add_track(self, rel, src, language="und", name="", default=False, forced=False, start_ms=0.0,
                  move=False):
        """! @brief Add an audio or subtitle file as a track (copied, or moved with `move`),
        named after its language. A file in a container (.m4a, .mka) is reduced to its raw
        stream. @return tracks(rel). @throws DemuxError (nothing changed)."""
        ap, m = self._split_item(rel)
        if not valid_language(language or "und"):
            raise DemuxError(f"{language!r} is not a language code (en, fr, pt-BR, ...)")
        info = identify(src)
        if len(info["tracks"]) != 1 or info["tracks"][0]["type"] not in ("audio", "subtitles"):
            raise DemuxError("the file must hold exactly one audio or subtitle track")
        t = info["tracks"][0]
        cid = (t.get("properties") or {}).get("codec_id") or \
            CODEC_NAMES.get(str(t.get("codec") or "").lower(), "")
        if cid == "S_TEXT/ASS" and os.path.splitext(src)[1].lower() == ".ssa":
            cid = "S_TEXT/SSA"
        table = AUDIO_CODECS if t["type"] == "audio" else SUB_CODECS
        if cid not in table:
            raise DemuxError(f"{cid or t.get('codec')} has no raw form here")
        d = os.path.dirname(ap)
        stem = os.path.splitext(os.path.basename(ap))[0]
        meta = {"type": t["type"], "codec": cid, "language": language or "und", "name": _clean(name),
                "default": bool(default), "forced": bool(forced), "start_ms": float(start_ms or 0)}
        fname = unique_name(stem, part_label(meta), table[cid], {n.lower() for n in os.listdir(d)})
        dst = os.path.join(d, fname)
        if os.path.splitext(src)[1].lower() == table[cid]:
            shutil.copy2(src, dst)
        else:
            ok, _o, err = _run(["mkvextract", src, "tracks", f"{t['id']}:{dst}"])
            if not ok or not os.path.exists(dst):
                raise DemuxError("mkvextract: " + (err or "").strip()[-200:])
        if meta["default"]:                        # one default per kind
            for other in m["tracks"]:
                if other["type"] == meta["type"]:
                    other["default"] = False
        m["tracks"].append({**meta, "file": fname})
        os.makedirs(self.work, exist_ok=True)
        test = os.path.join(self.work, uuid.uuid4().hex + ".mkv")
        err = mux_files(m, lambda n: ap if n == m["video"]["file"] else os.path.join(d, n), test)
        if os.path.exists(test):
            os.remove(test)
        if err:                                    # it must rebuild with the video
            os.remove(dst)
            raise DemuxError("it doesn't mux with the video: " + err)
        if move and os.path.exists(src):
            os.remove(src)
        write_manifest(manifest_path(ap), m)
        self.drop(rel)
        return self.tracks(rel)

    # -- uploads -------------------------------------------------------------------------
    def wanted(self, rel):
        """! @brief Should a newly stored video be split (its library chosen, tools present,
        not a public folder)."""
        return self.in_scope(rel) and available() and os.path.splitext(rel)[1].lower() not in ES_EXTS

    def enqueue(self, rel):
        with self.lock:
            if rel not in self.queue:
                self.queue.append(rel)
            if self.worker is None or not self.worker.is_alive():
                self.worker = threading.Thread(target=self._work, daemon=True, name="video-demux")
                self.worker.start()
        self.wake.set()

    def _work(self):
        while True:
            with self.lock:
                rel = self.queue.pop(0) if self.queue else None
            if rel is None:
                self.wake.clear()
                if not self.wake.wait(300):
                    return
                continue
            try:
                new_rel = self.demux_rel(rel)
                self.host.logger.info(f"separate streams: {rel} -> {new_rel} + tracks")
                self.after(new_rel, self.host.safe_path(self.host.media_dir, new_rel))
            except DemuxError as e:
                self.last_error[rel] = str(e)
                self.host.logger.info(f"separate streams: {rel} stays one file: {e}")
                ap = self.host.safe_path(self.host.media_dir, rel)
                if ap and os.path.exists(ap):
                    self.after(rel, ap)
            except Exception as e:
                self.host.logger.error(f"separate streams: {rel}: {e}")