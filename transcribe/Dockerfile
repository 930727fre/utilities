FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        mkvtoolnix \
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

# subtile-ocr — DVD-era bitmap subtitle OCR (used by the container_subs
# vobsub-ocr tier for older Blu-ray rips like Friends x265 Silence that
# preserved the original DVD VobSub track instead of stripping / re-OCRing
# it). Rust CLI by gwen-lg (fork of elizagamedev/vobsubocr), actively
# maintained; wraps leptonica + tesseract via `leptess` crate.
#
# Picked over ruediger/VobSub2SRT (C++, upstream dormant since 2013,
# doesn't build clean against GCC 12 + tesseract 5) and over Debian's
# own package (only in experimental, not in bookworm). No prebuilt
# binaries are published — install via `cargo install` and rip out the
# Rust toolchain after the build to keep the runtime image lean.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates build-essential pkg-config clang \
        libtesseract-dev libleptonica-dev \
    && curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
        | sh -s -- -y --profile minimal --default-toolchain stable --no-modify-path \
    && . "$HOME/.cargo/env" \
    && cargo install --locked subtile-ocr \
    && mv "$HOME/.cargo/bin/subtile-ocr" /usr/local/bin/ \
    && rm -rf "$HOME/.cargo" "$HOME/.rustup" \
    && apt-get purge -y curl build-essential pkg-config clang libtesseract-dev libleptonica-dev \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
