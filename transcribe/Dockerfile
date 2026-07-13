FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        gcc \
        g++ \
        python3-dev \
        tesseract-ocr \
        tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

# alass — subtitle-to-video alignment. Picked over ffsubsync because alass
# does piecewise drift detection: it can spot "candidate has an extra
# cold-open segment that the video doesn't have" and align the rest
# independently, instead of forcing a single uniform offset across the
# whole episode (which is what made ffsubsync misalign on releases with
# different opening structures).
RUN curl -fsSL -o /usr/local/bin/alass \
        https://github.com/kaegi/alass/releases/download/v2.0.0/alass-linux64 \
    && chmod +x /usr/local/bin/alass

# vobsub2srt — DVD-era bitmap subtitle OCR (used by the container_subs
# vobsub-ocr tier for older Blu-ray rips like Friends x265 Silence that
# preserved the original DVD VobSub track instead of stripping / re-OCRing
# it). Debian dropped the package after Bullseye (upstream unmaintained,
# tesseract API drift), so we build from source. If the build breaks on
# an API mismatch in a future tesseract update, either pin the base image
# tesseract version or switch to a maintained fork.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake pkg-config libtiff-dev libtesseract-dev git \
        autoconf automake \
    && git clone --depth 1 https://github.com/ruediger/VobSub2SRT.git /tmp/vs2s \
    && cd /tmp/vs2s \
    && (test -x ./configure || autoreconf -i) \
    && ./configure && make && make install \
    && cd / && rm -rf /tmp/vs2s \
    && apt-get purge -y build-essential cmake pkg-config libtiff-dev libtesseract-dev autoconf automake \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
