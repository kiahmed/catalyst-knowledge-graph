# robotics-social — DuckDB top-N cards → Postiz scheduling
# Cloud Run Job in prod. One-shot batch execution.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY main.py .

# Local HTTP surface for dev — lets `docker compose up` expose it and lets
# `make social` trigger it without rebuilding the container. In Cloud Run Job
# mode, the entry point becomes `python main.py --once` (see deploy.sh).
ENV PORT=8082
EXPOSE 8082

CMD ["python", "main.py", "--serve"]
