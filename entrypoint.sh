#!/bin/sh
# Container entrypoint. The Android APK is baked into the image at
# static/app/cim-family.apk (served at /static/app/cim-family.apk); also drop
# a copy into the host-mounted data/ volume so it's reachable from the host
# without exec-ing into the container.
if [ -f /app/static/app/cim-family.apk ]; then
    mkdir -p /app/data && cp -f /app/static/app/cim-family.apk /app/data/cim-family.apk 2>/dev/null \
        && echo "Android app: /app/data/cim-family.apk (host: ./data/cim-family.apk) and http://<host>:8000/static/app/cim-family.apk"
else
    echo "Android app not built into this image (build with --build-arg BUILD_ANDROID=1)"
fi
exec python manager.py
