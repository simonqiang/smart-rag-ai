FROM python:3.13-slim

# Tesseract with the spec §15 OCR language packs for image extraction.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-chi-sim \
        tesseract-ocr-chi-tra \
        tesseract-ocr-msa \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY infra/requirements.lock ./
COPY infra/requirements-torch-cpu.lock ./
RUN pip install --no-cache-dir --no-deps \
        --index-url https://download.pytorch.org/whl/cpu \
        -r requirements-torch-cpu.lock \
    && pip install --no-cache-dir -r requirements.lock
COPY src ./src
COPY apps ./apps
COPY config ./config
ENV PYTHONPATH=/app/src
EXPOSE 8000
