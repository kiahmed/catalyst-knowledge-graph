# robotics-ingest — GCS read → extract → resolve → DuckDB write
# Runs as a Cloud Functions Gen 2 (Python 3.12) in prod.
# Same image runs locally via functions-framework exposing port 8080.
#
# Build context is the REPO ROOT (see docker-compose.yml), so we can pull
# in src/ and config/ — both shared with dev-utils/.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY tools/robotics-ingest/requirements.txt .
RUN pip install -r requirements.txt

# Shared library + config. Matching tree layout on the container so
# `from src.ingest import run_ingest` works identically local + prod.
COPY src /app/src
COPY config /app/config
COPY tools/robotics-ingest/main.py /app/main.py

ENV PORT=8080
EXPOSE 8080

# functions-framework resolves the target function from this module.
CMD ["functions-framework", "--target=run_ingest", "--host=0.0.0.0", "--port=8080"]
