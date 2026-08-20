FROM python:3.12

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

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

COPY . .

EXPOSE 8000

CMD ["python", "manager.py"]