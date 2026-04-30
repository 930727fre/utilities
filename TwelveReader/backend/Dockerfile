FROM pytorch/pytorch:2.4.0-cuda12.1-cudnn9-runtime

RUN conda install -c conda-forge libsndfile -y && \
    apt-get update && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD mkdir -p /data/books /data/cache /data/static && uvicorn main:app --host 0.0.0.0 --port 8000
