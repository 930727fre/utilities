FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# NVENC-enabled ffmpeg from BtbN's static builds
# (https://github.com/BtbN/FFmpeg-Builds). Statically linked, portable
# across Linux distros, has NVENC/NVDEC/VAAPI baked in. Runtime CUDA libs
# come from nvidia-container-toolkit on the host (already in place for
# whisper).
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates xz-utils \
        aria2 gcc g++ python3-dev \
    && curl -fsSL \
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n7.1-latest-linux64-gpl-7.1.tar.xz" \
        -o /tmp/ffmpeg.tar.xz \
    && mkdir -p /tmp/ffmpeg \
    && tar xJf /tmp/ffmpeg.tar.xz -C /tmp/ffmpeg --strip-components=1 \
    && cp /tmp/ffmpeg/bin/ffmpeg /tmp/ffmpeg/bin/ffprobe /usr/local/bin/ \
    && rm -rf /tmp/ffmpeg /tmp/ffmpeg.tar.xz \
    && apt-get purge -y curl xz-utils \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
