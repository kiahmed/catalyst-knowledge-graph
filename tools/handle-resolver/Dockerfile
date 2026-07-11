# handle-resolver — per-entity social handle resolution (verify-or-abstain).
# Runs as a Cloud Functions Gen 2 (Python 3.12) in prod (Firestore cache).
# Same image runs locally via functions-framework on port 8083 (DuckDB cache).
#
# Build context is the REPO ROOT (see docker-compose.yml), so we can pull
# in src/ — shared with robotics-ingest.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY tools/handle-resolver/requirements.txt .
RUN pip install -r requirements.txt

COPY src /app/src
COPY tools/handle-resolver/main.py /app/main.py

ENV PORT=8083
EXPOSE 8083

CMD ["functions-framework", "--target=resolve_handles", "--host=0.0.0.0", "--port=8083"]
