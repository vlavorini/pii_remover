# ──────────────────────────────────────────────────────────────────────
# PII Remover - application image (CPU only, no build toolchain at runtime)
# ──────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# pypdfium2 ships wheels; libgomp/libgl are needed by its PDFium build on slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      curl \
      libgomp1 \
      libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Non-root runtime user; /data holds uploads, encrypted outputs and secrets
RUN useradd --create-home --uid 10001 appuser \
 && mkdir -p /data/uploads /data/outputs /data/scratch \
 && chown -R appuser:appuser /srv /data

USER appuser
ENV UPLOAD_DIR=/data/uploads \
    OUTPUT_DIR=/data/outputs \
    SCRATCH_DIR=/data/scratch \
    PORT=8000 \
    ROOT_PATH=

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD curl --fail http://localhost:8000/health || exit 1

CMD ["python", "-m", "app.main"]
