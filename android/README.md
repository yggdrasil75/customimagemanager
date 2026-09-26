# CIM Family — Android app

End-to-end encrypted photo backup + gallery for your own customimagemanager.
Same wire format as `modules/family_share/crypto.py`: X25519 + HKDF + AES-256-GCM,
so the server, your family's instances and this app all speak one protocol.

What it does
- **Backup folders** (Backup tab): every MediaStore bucket (Camera, Screenshots,
  WhatsApp Images, …) gets a policy — *Off*, *Keep* (upload, leave the copy on
  the phone) or *Upload & purge* (upload; later free it from the phone).
- Uploads run in the background (WorkManager): on a new-photo trigger, every 15
  minutes, and on "Back up now". Wi-Fi-only / charging-only / videos are toggles.
  Hashes are checked against the server in one batch so a first sync of a big
  library doesn't need one round trip per photo.
- **Free up space** (Settings): removes photos that are already on the server
  from *Upload & purge* folders. Android requires you to confirm the batch — the
  app cannot silently delete media it didn't create.
- **Library** tab: the server's whole library as a day-grouped grid, thumbnails
  and full images fetched sealed to this phone's key; videos open in the system
  player after decrypting to the app's private cache.
- Keys live in hardware-backed EncryptedSharedPreferences and are excluded from
  device backups.

Pairing
1. Server → Settings → 👪 Family share → add a peer, kind **my phone**, name e.g.
   `pixel`, choose the library folder its uploads land in.
2. Click **Pairing code for them**, get the `fs1.…` string to the phone (any
   channel; it contains the secret the phone uses to reach the server, so not a
   public forum), paste it in the app, tap **Pair**.
3. The app shows *its* pairing code (QR + text). Paste it into the peer's row on
   the server. Compare the fingerprints. Done.

Building
```
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
- No in-app video player (system player via FileProvider).
- Thumbnails and viewed media are cached decrypted in the app's private cache
  (Settings → Clear cached thumbnails / media).
- The app has not been compiled in the environment that wrote it; the first
  `./android/build.sh` run is the compile.
