#!/usr/bin/env bash
# Build the CIM Family Android app (release APK) with nothing but a JDK: this
# script fetches the Android command-line tools + SDK packages + Gradle into
# $ANDROID_SDK_ROOT / $GRADLE_HOME (cached between runs when those dirs persist),
# generates a release keystore on first use, and writes the signed APK to
# $OUT (default: ../static/app/cim-family.apk, where the server serves it).
#
#   ./android/build.sh                 # host build (needs JDK 17, curl, unzip)
#   docker build --build-arg BUILD_ANDROID=1 .   # same thing inside the image
#
# KEEP android/keystore/ (gitignored). An APK signed with a different key will
# not install over the previous one — back it up with your app_config.json.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${OUT:-$HERE/../static/app/cim-family.apk}"
ANDROID_SDK_ROOT="${ANDROID_SDK_ROOT:-$HERE/.sdk}"
GRADLE_HOME="${GRADLE_HOME:-$HERE/.gradle-dist}"
GRADLE_VERSION="${GRADLE_VERSION:-8.9}"
CMDLINE_TOOLS_URL="${CMDLINE_TOOLS_URL:-https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip}"
export ANDROID_SDK_ROOT ANDROID_HOME="$ANDROID_SDK_ROOT"

command -v java >/dev/null || { echo "build.sh: needs a JDK 17 (java on PATH)"; exit 1; }

# ── SDK ──────────────────────────────────────────────────────────────────────
if [ ! -x "$ANDROID_SDK_ROOT/cmdline-tools/latest/bin/sdkmanager" ]; then
    echo "==> fetching Android command-line tools"
    mkdir -p "$ANDROID_SDK_ROOT/cmdline-tools"
    tmp="$(mktemp -d)"
    curl -fsSL "$CMDLINE_TOOLS_URL" -o "$tmp/tools.zip"
    unzip -q "$tmp/tools.zip" -d "$tmp"
    rm -rf "$ANDROID_SDK_ROOT/cmdline-tools/latest"
    mv "$tmp/cmdline-tools" "$ANDROID_SDK_ROOT/cmdline-tools/latest"
    rm -rf "$tmp"
fi
SDKM="$ANDROID_SDK_ROOT/cmdline-tools/latest/bin/sdkmanager"
yes | "$SDKM" --licenses >/dev/null 2>&1 || true
"$SDKM" --install "platform-tools" "platforms;android-34" "build-tools;34.0.0" >/dev/null

# ── Gradle ───────────────────────────────────────────────────────────────────
if [ ! -x "$GRADLE_HOME/bin/gradle" ]; then
    echo "==> fetching Gradle $GRADLE_VERSION"
    tmp="$(mktemp -d)"
    curl -fsSL "https://services.gradle.org/distributions/gradle-${GRADLE_VERSION}-bin.zip" -o "$tmp/gradle.zip"
    unzip -q "$tmp/gradle.zip" -d "$tmp"
    # $GRADLE_HOME may be a docker cache mount: clear its contents, never the dir.
    mkdir -p "$GRADLE_HOME"; find "$GRADLE_HOME" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
    cp -a "$tmp/gradle-${GRADLE_VERSION}/." "$GRADLE_HOME/"; rm -rf "$tmp"
fi

# ── keystore (generated once, then reused) ───────────────────────────────────
KS_DIR="$HERE/keystore"
mkdir -p "$KS_DIR"
if [ ! -f "$KS_DIR/release.jks" ]; then
    echo "==> generating release keystore in $KS_DIR (keep it!)"
    pw="${CIM_KEYSTORE_PASSWORD:-$(head -c 24 /dev/urandom | base64 | tr -d '/+=')}"
    keytool -genkeypair -v -keystore "$KS_DIR/release.jks" -alias cim -keyalg RSA -keysize 4096 -validity 10000 \
        -storepass "$pw" -keypass "$pw" -dname "CN=CIM Family, O=customimagemanager" >/dev/null 2>&1
    printf 'storeFile=release.jks\nstorePassword=%s\nkeyAlias=cim\nkeyPassword=%s\n' "$pw" "$pw" > "$KS_DIR/keystore.properties"
    chmod 600 "$KS_DIR/keystore.properties"
fi

# ── build ────────────────────────────────────────────────────────────────────
export CIM_APP_VERSION_CODE="${CIM_APP_VERSION_CODE:-$(date +%Y%m%d%H)}"
export CIM_APP_VERSION_NAME="${CIM_APP_VERSION_NAME:-1.0.$(date +%Y%m%d)}"
echo "==> gradle assembleRelease (version $CIM_APP_VERSION_NAME / $CIM_APP_VERSION_CODE)"
(cd "$HERE" && "$GRADLE_HOME/bin/gradle" --no-daemon -q assembleRelease)
mkdir -p "$(dirname "$OUT")"
cp "$HERE/app/build/outputs/apk/release/app-release.apk" "$OUT"
echo "==> APK: $OUT"
