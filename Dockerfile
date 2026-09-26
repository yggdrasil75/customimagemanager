# ── optional stage: the Android app (BUILD_ANDROID=1) ─────────────────────────
# Builds android/ into static/app/cim-family.apk with a JDK + the Android SDK
# fetched by android/build.sh. Off by default because it pulls ~1.5 GB of SDK;
# the runtime image copies whatever this stage produced (an empty dir when off).
#   docker build --build-arg BUILD_ANDROID=1 -t cim .
# Signing: commit-or-copy a keystore into android/keystore/ BEFORE building
# (see android/README.md); without one the stage generates a fresh key every
# build and phones refuse to update over an APK signed with a different key.
FROM eclipse-temurin:17-jdk AS android
ARG BUILD_ANDROID=0
RUN apt-get update && apt-get install -y --no-install-recommends curl unzip && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY android/ ./android/
RUN --mount=type=cache,target=/build/android/.sdk \
    --mount=type=cache,target=/build/android/.gradle-dist \
    --mount=type=cache,target=/root/.gradle \
    mkdir -p /build/static/app && \
    if [ "$BUILD_ANDROID" = "1" ]; then OUT=/build/static/app/cim-family.apk ./android/build.sh; \
    else echo "BUILD_ANDROID=0: skipping the Android app"; fi

FROM python:3.12
ARG GPU_BACKEND=cpu

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    YOLO_CONFIG_DIR=/app/data

# System deps. The ultralight profile is an image VIEWER only, so it skips the
# heavy CLIs/libs (ffmpeg, calibre, opengl, boost) and keeps just what the core
# metadata module and JXL thumbnails need: libjxl-tools + libexiv2.
# Every other backend (cpu/cuda/rocm) gets the full set for the ML stack.
RUN apt-get update \
    && if [ "${GPU_BACKEND}" = "ultralight" ]; then \
        apt-get install -y --no-install-recommends \
            libjxl-tools \
            libexiv2-dev \
            libboost-python-dev \
            libgomp1 ; \
    else \
        apt-get install -y --no-install-recommends \
            libjxl-tools \
            ffmpeg \
            libgl1 \
            libglib2.0-0 \
            libexiv2-dev \
            libboost-python-dev \
            libgomp1 \
            unrar-free \
            p7zip-full \
            calibre ; \
    fi \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements-cpu.txt requirements-cuda.txt requirements-rocm.txt requirements-ultralight.txt ./
# ultralight installs ONLY its minimal pin set (no requirements.txt heavy ML
# stack). All other backends layer the accelerator wheels on top of the full
# requirements.txt as before.
RUN echo "Installing backend: ${GPU_BACKEND}" \
    && if [ "${GPU_BACKEND}" = "ultralight" ]; then \
        pip install -r requirements-ultralight.txt ; \
    else \
        pip install -r "requirements-${GPU_BACKEND}.txt" -r requirements.txt ; \
    fi

RUN if [ "${GPU_BACKEND}" = "rocm" ]; then \
        apt install migraphx half \
        && pip uninstall -y onnxruntime onnxruntime-gpu onnxruntime-rocm onnxruntime-migraphx || true \
        && pip install onnxruntime-migraphx \
            -f https://repo.radeon.com/rocm/manylinux/rocm-rel-7.2.0/ \
        && pip install onnxruntime-rocm \
            -f https://repo.radeon.com/rocm/manylinux/rocm-rel-7.2.0/ ; \
    fi

COPY . .
# The app built (or skipped) by the android stage, served at /static/app/cim-family.apk.
COPY --from=android /build/static/app/ ./static/app/

RUN mkdir -p static && curl -fsSL https://cdn.tailwindcss.com/3.4.17 -o static/tailwindcss.js

RUN mkdir -p static/vendor && \
    curl -fsSL https://unpkg.com/three@0.137.5/build/three.min.js \
        -o static/vendor/three.min.js && \
    curl -fsSL https://unpkg.com/three@0.137.5/examples/js/loaders/OBJLoader.js \
        -o static/vendor/OBJLoader.js && \
    curl -fsSL https://unpkg.com/three@0.137.5/examples/js/controls/OrbitControls.js \
        -o static/vendor/OrbitControls.js && \
    curl -fsSL https://unpkg.com/drawflow@0.0.60/dist/drawflow.min.js \
        -o static/vendor/drawflow.min.js && \
    curl -fsSL https://unpkg.com/drawflow@0.0.60/dist/drawflow.min.css \
        -o static/vendor/drawflow.min.css

EXPOSE 8000

CMD ["python", "manager.py"]