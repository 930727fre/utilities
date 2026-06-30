FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        aria2 \
        curl \
        gcc \
        g++ \
        python3-dev \
        mkvtoolnix \
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

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
