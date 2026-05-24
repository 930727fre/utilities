FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cu121 && \
    pip install --no-cache-dir setuptools && \
    pip install --no-cache-dir --no-build-isolation openai-whisper==20231117 && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

