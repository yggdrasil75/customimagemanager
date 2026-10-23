# Phone API (family_share inbound, v2)

Everything a paired phone can do goes through `/api/family_share/inbound/*`,
authenticated by `X-Family-Peer` + `X-Family-Key` like the existing endpoints,
and runs **as the account that owns the phone** (feature permissions, ownership
filters and quotas apply exactly as they do for that account in the browser).
Only `kind='device'` peers may use these (`_phones_only`).

Every response body is **sealed to the phone** (`_sealed_response`: framed
AES-GCM stream, `X-Family-Env` = envelope header, `X-Family-Mime` = real mime).
Every POST body is a **sealed request** `{"env": <header>, "meta": <sealed inner>}`
(`_sealed_request`), inner carries `ts` / `to` / `from` like `have` does.
Query strings stay plaintext, as `timeline` / `thumb` / `media` already do.

Existing v1 endpoints (`ping`, `push`, `have`, `timeline`, `thumb`, `media`, ...)
are unchanged.

Refusals made before anything runs (no / wrong key -> 401, not a phone or a
feature missing -> 403 with `error_code: "forbidden"`, off the proxy list -> 404,
a plaintext POST -> 400, `frames` range errors -> 400 / 416) are **plain JSON**
without `X-Family-Env`, like the v1 endpoints' refusals. Everything else is
sealed. A client tells them apart by the presence of `X-Family-Env`.

## 1. `GET inbound/caps`

Sealed JSON:
```json
{"ok": true, "api": 2, "chunk": 1048576,
 "modules": {"gallery": true, "music": true, "radio": true, "playlists": true,
             "tv": true, "movies": true, "books": true},
 "account": {"username": "ann", "display_name": "Ann"}}
```
A module is `true` only when it is loaded on the server AND the phone's account
holds `read` on its feature (`tab.gallery`, `tab.music`, `tab.tv`, `tab.movies`,
`tab.books`; radio/playlists follow `tab.music`). The phone shows one tab per
true module. A server without `caps` (404) is a v1 server: gallery only.
With sign-in off the phone runs as nobody in particular: `account` is
`{"username": "", "display_name": ""}` and every loaded module is `true`.

## 2. `GET inbound/frames?p=<rel>&first=<N>&count=<M>` - ranged sealed media

The file's plaintext is cut into 1 MiB chunks (`crypto.CHUNK`); chunk `i` is
sealed with nonce `i` and AAD `file:<i>:<last>` exactly as in a full stream. This
endpoint returns only frames `N .. N+M-1` (clamped to the file; `M` <= 64,
default 4). Headers (besides `X-Family-Env` / `X-Family-Mime`):

| header | value |
|---|---|
| `X-Family-Size`   | total plaintext bytes of the file |
| `X-Family-Frames` | total frame count = max(1, ceil(size / CHUNK)) |
| `X-Family-First`  | N (the index of the first frame in the body) |

Each response has its own envelope (new ephemeral key), so frames from two
responses are never mixed. The client opens frames starting at index `N`
(`Opener.openFrames(input, first, expectedCount, totalFrames)`), and MUST check:
exactly `min(M, frames - N)` frames arrived, and the `last` flag is set on frame
`frames-1` and on no other. That binds the total length, so truncation is still
detected. `N >= frames` -> 416 JSON error (`{"ok": false, "error", "size", "frames"}`);
`N < 0` or a non-integer `first` / `count` -> 400; `count` outside 1..64 is
clamped. An empty file is one frame holding 0 bytes. Byte offset `b` lives in frame
`b / CHUNK` at offset `b % CHUNK`; that is all an ExoPlayer DataSource needs.

Permission: the path must pass `host.check_path(rel)` for the account, and the
account needs `read` on the feature of the module that owns the file kind
(gallery for images/video in the library, music for audio, tv/movies for their
files, books for books) - in practice: any one of those features plus
`check_path`.

`inbound/media` (whole file) stays for v1 phones.

## 3. `GET|POST inbound/api/<path>` - sealed proxy to the web API

The phone reaches the server's existing JSON API through one sealed proxy, so
response shapes are **identical to the browser's** `/api/<path>` and are
documented by the modules themselves. Only an explicit allowlist is reachable:

GET (query string forwarded as-is):
- `music/status`, `music/artists`, `music/albums`, `music/songs`, `music/genres`,
  `music/track`, `music/cover/<path>`, `music/radio/status`, `music/playlists`,
  `music/playlist`
- `tv/status`, `tv/shows`, `tv/seasons`, `tv/episodes`, `tv/episode`,
  `tv/subtitle/<path>`
- `movies`, `movies/item`, `movies/genres`, `movies/status`,
  `movies/subtitle/<path>`
- `books/status`, `books/list`, `books/authors`, `books/series`, `books/detail`,
  `books/cover/<path>` (read-only browsing; `PROXY_GET` in module.py is the
  source of truth)

`<path>` matches any non-empty rest of the path (URL-encode it as the browser
does). Any path the call names (`<path>`, `?rel_path=`, a body's `rel_path`)
must pass `host.check_path` for the account, else a sealed 404 - the same rule
the books module applies to its own routes.

POST (sealed inner `{"body": {...}}` forwarded as the JSON request body):
- `music/radio/next`, `music/radio/played`, `music/shuffle`, `music/similar`
- `music/playlists/save`, `music/playlists/delete`

The proxy dispatches to the real Flask view (same feature/level gate the route
was registered with, running as the phone's account), then seals whatever it
returned: JSON comes back as sealed JSON (mime `application/json`), binary
(covers, subtitles) as sealed bytes with its mime. HTTP status is preserved,
so a view's own `{"success": false, ...}` 403 / 404 arrives sealed with that
status. A music video's cover (the web route redirects to its thumbnail) comes
back as the thumbnail itself. A non-JSON body over 20 MB is refused with a
sealed 413 - fetch such files through `frames`.
Streaming routes (`music/stream/...`) are NOT proxied - use `frames`.
Anything off the list -> 404 `{"ok": false, "error": "not available to phones"}`.

## 4. Playlists (new, music module)

Per account (owner = signed-in username, `""` with sign-in off).

- `GET /api/music/playlists` -> `{"success": true, "playlists": [{"id", "name", "count", "seconds", "updated_at"}]}`
- `GET /api/music/playlist?id=<id>` -> `{"success": true, "playlist": {"id", "name", "updated_at"}, "songs": [<same song objects as /api/music/songs>]}`
- `POST /api/music/playlists/save` `{"id"?: int, "name": str, "items": [rel_path, ...]}` -> `{"success": true, "id": int}`
  (no id = create; items replace the whole list, order kept, duplicates allowed)
- `POST /api/music/playlists/delete` `{"id": int}` -> `{"success": true}`

`count` / `seconds` cover the songs `/api/music/playlist` would return for the
account (tracks it can't see, or no longer indexed, are skipped; paths it can't
see are dropped on save). Unknown or another account's id -> 404, missing name
-> 400. Save / delete need only `read` on `tab.music` (a listener's own state,
like `radio/played`; `write` on `tab.music` means editing the library's tags).

The web Music tab gets a Playlists view (create, add from song rows / queue,
reorder, delete). The phone can download a whole playlist for offline use.

## Song objects

Whatever `/api/music/songs` returns today; the phone relies on at least
`rel_path`, `title`, `artist`, `album`, `duration`, and fetches covers through
`inbound/api/music/cover/<rel_path>`.

Today that is `{rel_path, title, artist, album, albumartist, track, disc, year,
genre, composer, comment, duration, bitrate, samplerate, channels, cluster,
tags, has_emb, has_cover, size, created}` (`has_cover` true / false / null =
not checked yet; similar / shuffle / semantic results add `score`). Audio bytes
come through `inbound/frames?p=<rel_path>`.
