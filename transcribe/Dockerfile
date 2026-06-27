FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# jellyfin's apt repo ships jellyfin-ffmpeg7 with NVENC compiled in +
# bundled CUDA libs. Container picks up the host's nvidia driver via
# nvidia-container-toolkit (which the host already has for whisper).
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl gnupg ca-certificates \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://repo.jellyfin.org/jellyfin_team.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/jellyfin.gpg \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/jellyfin.gpg] https://repo.jellyfin.org/debian bookworm main" \
        > /etc/apt/sources.list.d/jellyfin.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        jellyfin-ffmpeg7 \
        aria2 \
        gcc \
        g++ \
        python3-dev \
    && apt-get purge -y curl gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# jellyfin-ffmpeg7 installs binaries to /usr/lib/jellyfin-ffmpeg/.
ENV PATH=/usr/lib/jellyfin-ffmpeg:$PATH

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
