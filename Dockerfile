# LegalMind API server.
#
#   docker build -t legalmind-backend .
#   docker run --env-file .env -p 8000:8000 legalmind-backend
#
# The models are downloaded while the image is built, because the server only
# ever reads them from the local cache. Build arguments:
#   SKIP_RERANKER=true    leave out the 2.3 GB reranker (set QA_RERANK_MODE=off)
#   DOWNLOAD_MODELS=false build without models and mount a cache at /opt/models
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/opt/models \
    TOKENIZERS_PARALLELISM=false

WORKDIR /app

# CPU-only PyTorch first, so the multi-gigabyte CUDA build is not pulled in.
COPY requirements.txt .
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install -r requirements.txt

COPY alembic.ini .
COPY alembic ./alembic
COPY app ./app
COPY scripts ./scripts
COPY data ./data

ARG DOWNLOAD_MODELS=true
ARG SKIP_RERANKER=false
RUN mkdir -p /opt/models \
 && if [ "$DOWNLOAD_MODELS" = "true" ]; then \
      if [ "$SKIP_RERANKER" = "true" ]; then python -m scripts.pipeline.download_models --skip-reranker; \
      else python -m scripts.pipeline.download_models; fi; \
    fi

RUN useradd --create-home --uid 10001 legalmind \
 && mkdir -p /app/.cache /app/uploads \
 && chown -R legalmind:legalmind /app/.cache /app/uploads /opt/models
USER legalmind

ENV ENVIRONMENT=production
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=4).status != 200)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
