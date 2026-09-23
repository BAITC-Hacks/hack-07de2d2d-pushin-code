# Backend image. Build context is the REPOSITORY ROOT (it needs data/, models/, outputs/):
#   docker build -f backend/Dockerfile -t windcast-backend .
#   compose: build: {context: .., dockerfile: backend/Dockerfile}
FROM python:3.12-slim

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# LightGBM needs the OpenMP runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ backend/
COPY data/ data/
COPY models/ models/
COPY outputs/ outputs/

ENV WINDCAST_ROOT=/app \
    PYTHONPATH=/app/backend \
    PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=6 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"]

# One worker on purpose: agent runs and their SSE streams live in this process's memory.
CMD ["uvicorn", "windcast.api:app", "--host", "0.0.0.0", "--port", "8000"]
