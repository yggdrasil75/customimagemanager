FROM python:3.12

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    YOLO_CONFIG_DIR=/app/data/Ultralytics

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

COPY requirements.txt .
# opencv-python-headless: cv2 is imported by manager.py but missing from requirements.txt
RUN pip install --no-cache-dir opencv-python-headless \
    && pip install --no-cache-dir -r requirements.txt

# RUN mkdir -p /app/models/smplx && \
#     curl -fSL -o /app/models/smplx/smplest_x_h.pth.tar \
#         "https://huggingface.co/waanqii/SMPLest-X/resolve/main/smplest_x_h.pth.tar" || \
#     echo "SMPLest-X checkpoint download skipped — mesh estimator will be disabled"

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