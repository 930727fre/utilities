FROM pytorch/pytorch:2.4.0-cuda12.1-cudnn9-runtime

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 \
        poppler-utils \
        fonts-noto-cjk \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libcairo2 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "marker-pdf[full]" && \
    pip install --no-cache-dir --upgrade torchvision

WORKDIR /work
