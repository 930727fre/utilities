# Multi-stage: pull NVENC-enabled ffmpeg + its bundled CUDA runtime libs
# from jellyfin's published image. They build ffmpeg with --enable-nvenc
# / --enable-cuda-llvm and ship the matching CUDA libs in a portable
# /usr/lib/jellyfin-ffmpeg tree.
FROM jellyfin/jellyfin-ffmpeg7:latest AS ffmpeg-stage

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH=/usr/lib/jellyfin-ffmpeg:$PATH \
    LD_LIBRARY_PATH=/usr/lib/jellyfin-ffmpeg/lib

COPY --from=ffmpeg-stage /usr/lib/jellyfin-ffmpeg /usr/lib/jellyfin-ffmpeg

RUN apt-get update && apt-get install -y --no-install-recommends \
        aria2 \
        gcc \
        g++ \
        python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
