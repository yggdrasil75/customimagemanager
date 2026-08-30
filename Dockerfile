FROM python:3.12
ARG GPU_BACKEND=cpu

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    YOLO_CONFIG_DIR=/app/data

# System deps: cjxl (libjxl-tools) + libs for opencv, pymupdf, rawpy, pyexiv2,
# video, insightface + calibre (ebook-convert / calibredb CLI)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libjxl-tools \
        ffmpeg \
        libgl1 \
        libglib2.0-0 \
        libexiv2-dev \
        libboost-python-dev \
        libgomp1 \
        unrar-free \
        p7zip-full \
        calibre \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements-cpu.txt requirements-cuda.txt requirements-rocm.txt ./
RUN echo "Installing GPU backend: ${GPU_BACKEND}" \
    && pip install -r "requirements-${GPU_BACKEND}.txt" -r requirements.txt

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