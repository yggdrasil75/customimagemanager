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

ARG SMPLESTX_REF=main
RUN pip install --no-cache-dir "git+https://github.com/SMPLCap/SMPLest-X.git@${SMPLESTX_REF}" || \
    echo "SMPLest-X install skipped (unavailable) — mesh estimator will be disabled"
RUN mkdir -p /app/models/smplx && \
    curl -fSL -o /app/models/smplx/smplest_x_h.pth.tar \
        "https://github.com/SMPLCap/SMPLest-X/releases/download/v1.0/smplest_x_h.pth.tar" || \
    echo "SMPLest-X checkpoint download skipped — mesh estimator will be disabled"

COPY . .

RUN mkdir -p static && curl -fsSL https://cdn.tailwindcss.com -o static/tailwindcss.js

EXPOSE 8000

CMD ["python", "manager.py"]