# robotics-admin — data editor API (Cloud Run, scales to zero).
# Build context = staged dir with src/, config/ and this tool (dev-utils/deploy.sh).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY tools/robotics-admin/requirements.txt .
RUN pip install -r requirements.txt

COPY src/ ./src/
COPY config/ ./config/
COPY tools/robotics-admin/main.py .

# One process: edits are serialized by an in-process lock (and max 1 instance).
CMD exec gunicorn --bind ":${PORT:-8082}" --workers 1 --threads 4 --timeout 300 main:app
