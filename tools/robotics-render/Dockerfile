# robotics-render — HTML card → PNG via Playwright/Chromium
# Cloud Run service in prod. Same image locally.
#
# Base image ships Chromium + deps pre-installed, saving ~300MB of apt layers.

FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Build context is the REPO ROOT (compose + deploy.sh agree) so the
# repo-root card templates can be baked in unconditionally — glob tricks
# break on Cloud Build's classic builder. See .dockerignore for the lean
# context.
COPY tools/robotics-render/requirements.txt .
RUN pip install --break-system-packages -r requirements.txt

COPY tools/robotics-render/main.py .

# Card templates baked at the TEMPLATES_DIR default so prod (no volume
# mounts on Cloud Run) can render. Local compose still mounts
# ./templates/cards over /templates — identical content wins either way.
COPY templates/cards/ /templates/

# Cloud Run injects $PORT; default 8081 for local dev to avoid collision with robotics-ingest.
ENV PORT=8081
EXPOSE 8081

# Gunicorn for prod-shape HTTP; single worker because Chromium is heavy.
# Concurrency is controlled at the Cloud Run service level (concurrency=1).
CMD ["sh", "-c", "exec gunicorn --bind :${PORT} --workers 1 --threads 2 --timeout 120 main:app"]
