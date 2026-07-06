# Minimal container that owns the DuckDB data volume and provides a CLI.
# DuckDB is embedded — this service exists to hold the volume + expose an
# interactive shell via `docker compose exec duckdb duckdb /data/robotics.duckdb`.

FROM debian:12-slim

ARG DUCKDB_VERSION=1.2.2
ARG TARGETARCH

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    case "$TARGETARCH" in \
        amd64) DUCKDB_ARCH="amd64" ;; \
        arm64) DUCKDB_ARCH="aarch64" ;; \
        *) echo "Unsupported arch: $TARGETARCH"; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/duckdb/duckdb/releases/download/v${DUCKDB_VERSION}/duckdb_cli-linux-${DUCKDB_ARCH}.zip" -o /tmp/duckdb.zip; \
    unzip /tmp/duckdb.zip -d /usr/local/bin; \
    rm /tmp/duckdb.zip; \
    chmod +x /usr/local/bin/duckdb

RUN useradd --system --create-home --uid 10001 duck && \
    mkdir -p /data /init && \
    chown -R duck:duck /data /init

VOLUME ["/data"]
WORKDIR /data
USER duck

COPY --chown=duck:duck init.sql /init/init.sql

# Default command: initialize schema if not present, then keep the container
# alive so exec'ing into it is trivial. Idempotent — init.sql uses IF NOT EXISTS.
CMD ["sh", "-c", "duckdb /data/robotics.duckdb < /init/init.sql && tail -f /dev/null"]
