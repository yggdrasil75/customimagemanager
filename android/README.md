# CIM Family - Android app

End-to-end encrypted photo backup + gallery for your own customimagemanager, plus
your server's music, TV shows, movies and books - one app, a tab per server module.
Same wire format as `modules/family_share/crypto.py`: X25519 + HKDF + AES-256-GCM,
so the server, your family's instances and this app all speak one protocol.

What it does
- **Backup folders** (Backup tab): every MediaStore bucket (Camera, Screenshots,
  WhatsApp Images, ...) gets a policy - *Off*, *Keep* (upload, leave the copy on
  the phone) or *Upload & purge* (upload; later free it from the phone).
- Uploads run in the background (WorkManager): on a new-photo trigger, every 15
  minutes, and on "Back up now". Wi-Fi-only / charging-only / videos are toggles.
  Hashes are checked against the server in one batch so a first sync of a big
  library doesn't need one round trip per photo.
- **Free up space** (Settings): removes photos that are already on the server
  from *Upload & purge* folders. Android requires you to confirm the batch - the
  app cannot silently delete media it didn't create.
- **Photos** tab -> *Library*: the server's whole library as a day-grouped grid,
  thumbnails and full images fetched sealed to this phone's key; videos play in
  the in-app player (streamed, decrypted on the fly) on a v2 server, or open in
  the system player after decrypting to the app's private cache on an older one.
- Keys live in hardware-backed EncryptedSharedPreferences and are excluded from
  device backups.

Music, video and books (servers with the v2 phone API)
- The tabs follow the server: on start the app asks `inbound/caps`
  (`modules/family_share/PHONE_API.md`) and shows Music / Video / Books only for
  modules that are loaded there and that the phone's account may use. A server
  without `caps` is treated as photos-only. The answer is remembered, so the app
  opens on the right tabs offline.
- **Music**: Radio, Offline, Playlists, Albums, Artists, Songs (search, incl.
  `sem:` sound search), Genres; covers decrypted and cached. Tap to play, or
  *Play next* / *Add to queue*. A mini player sits above the tabs; tap it for
  Now Playing (cover, seek bar, previous / play / next, the queue).
- Playback runs in a Media3 `MediaSessionService` (`media/PlaybackService.kt`), so
  music keeps playing in the background with the media notification, lock-screen
  and Bluetooth / headset controls.
- **Radio**: *Start radio* plays a round from `music/radio/next`; when fewer than
  3 tracks are left the next round is appended; each track listened to for over
  30 s or half its length is reported (`music/radio/played`) so the server can
  space repeats. *Resume round N* replays the server's current round. Errors from
  the server (no audio embedding model, ...) are shown as-is.
- **Downloads**: *Download* on a playlist, an album or the current radio round
  pulls every track (and its cover) into an offline store in the app's files dir
  (WorkManager; Wi-Fi only by default). *Offline* lists them with progress, plays
  them with no server connection, retries failures and deletes. Playback always
  prefers the downloaded copy.
- **Video**: TV shows -> seasons -> episodes, and Movies (search, genre filter).
  The in-app player (media3 PlayerView) loads the server's subtitle files
  (WebVTT) and remembers the position per file. Music pauses while a video plays.
- **Books**: list / search with covers; *Open* downloads the file (decrypted, to
  the app cache) and hands it to a reader app. There is no in-app reader.

How streaming works
- Media is read through `inbound/frames` a few 1 MiB frames at a time; every
  slice is authenticated (`Crypto.Opener.openFrames`: exact frame count, final
  flag only on the file's last frame, nothing trailing), so a seek is just a
  request for the frame that holds the new position (`media/SealedFrameDataSource.kt`).
- Two caches (`media/MediaCache.kt`): a streaming cache in the app's cache dir
  (least recently used goes first; size in Settings) and the never-evicted offline
  store. The player reads offline -> cache -> server.
- Read-ahead: while the queue plays, the rest of the current track and the next
  ones are fetched into the cache until *Read ahead: music* minutes (default 30)
  are there; for a video, *Read ahead: video* minutes (default 10) of it.

Settings (Music & video, v2 servers)
- Read ahead: music (minutes, default 30) / video (default 10).
- Cache size (256 MB - 4 GB, default 1 GB; applies after restarting the app).
- Stream over mobile data (on) / Read ahead over mobile data (off) /
  Downloads only on Wi-Fi (on).
- Clear cache (streaming cache and covers; downloads stay).

Pairing
1. Server, signed in as yourself -> Settings -> **My devices** -> add the phone
   (name e.g. `ann-pixel`; optionally a folder; what it sees: your personal
   folder only, or everything your account can see). An admin can also pair a
   phone for any account in Settings -> Family share.
2. Click **Code for the app**, get the `fs1....` string to the phone (any
   channel; it contains the secret the phone uses to reach the server, so not a
   public forum), paste it in the app, tap **Pair**.
3. The app shows *its* pairing code (QR + text). Paste it into the phone's row on
   the server. Compare the fingerprints. Done.

The phone acts as the account that paired it: the Library tab shows what that
account may see, uploads land in its personal folder and count against its
quota, and Settings shows "Paired as <account>". If the account is disabled the
server refuses the phone and the app says so (backup pauses until it is
re-enabled); if the account is deleted its phones are removed and must be paired
again from another account. Phones paired before accounts existed were given to
the server's first admin and keep working unchanged.

Away from home
- When the paired server URL doesn't answer (you're not on the home network and
  the server has no public address), the app finds the server through the
  server's **Remote access** module: rendezvous gateways and the BitTorrent DHT
  tell each side where the other is, both punch through their NATs, and the app
  talks to the server directly over an end-to-end encrypted UDP link
  (`Link.kt`, same protocol as `modules/remote_access/link.py`). No relay ever
  carries photos. `Remote.kt` picks direct or link per request; `Api` doesn't
  know the difference.
- The app learns the server's gateway list whenever it reaches the server
  directly. Settings -> *Away from home* turns it off and tests the link.
- When both ends sit behind strict (symmetric) NATs no link forms; backup waits
  until the phone is home again. IPv6 on both ends avoids NAT entirely.

Building
```
./install.sh                                        # native install builds it when android/ changed
./android/build.sh                                  # JDK 17 + curl + unzip on the host
docker build --build-arg BUILD_ANDROID=1 -t cim .   # or inside the image
BUILD_ANDROID=1 docker compose build cim
```
The APK lands in `static/app/cim-family.apk` and is served by the app at
`/static/app/cim-family.apk` (link in the Family share settings tab).

**Keep `android/keystore/`.** It is generated on the first build and gitignored.
Phones only update over an APK signed with the same key; losing it means
uninstall/reinstall (the app's keys and upload bookkeeping go with it, so
re-pair and re-scan). For the docker build, copy an existing keystore into
`android/keystore/` before `docker build`.

Not done yet / known limits
- Thumbnails, covers, cached media and opened books are stored decrypted in the
  app's private storage (Settings -> Clear cache / Clear cached thumbnails).
- Video plays only while its screen is open (no background / picture-in-picture);
  only music uses the background service.
- Playlists can be played and downloaded but not edited on the phone (edit them
  in the web Music tab).
- Books open in an external reader app; the phone needs one for the format.
- Posters and artist pictures are loaded as the v1 thumbnail; an account without
  the gallery feature falls back to fetching small image files whole.
- A change of the cache size takes effect at the next app start.
- The app has not been compiled in the environment that wrote it (no Android SDK
  there; the non-Compose code was type-checked against Media3 1.4.1 sources); the
  first `./android/build.sh` / CI run is the compile.