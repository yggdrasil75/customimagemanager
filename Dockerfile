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

RUN mkdir -p static && curl -fsSL https://cdn.tailwindcss.com/3.4.17 -o static/tailwindcss.js

RUN mkdir -p static/vendor && \
    curl -fsSL https://unpkg.com/three@0.137.5/build/three.min.js \
        -o static/vendor/three.min.js && \
    curl -fsSL https://unpkg.com/three@0.137.5/examples/js/loaders/OBJLoader.js \
        -o static/vendor/OBJLoader.js && \
    curl -fsSL https://unpkg.com/three@0.137.5/examples/js/controls/OrbitControls.js \
        -o static/vendor/OrbitControls.js

EXPOSE 8000

CMD ["python", "manager.py"]