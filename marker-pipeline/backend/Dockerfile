FROM pytorch/pytorch:2.4.0-cuda12.1-cudnn9-runtime

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 \
        poppler-utils \
        fonts-noto-cjk \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libcairo2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir --upgrade torchvision

COPY . .

EXPOSE 8000

CMD mkdir -p /data && uvicorn main:app --host 0.0.0.0 --port 8000
