# notion-reports production image. Build on the Intel Mac mini: linux/amd64.
FROM --platform=linux/amd64 python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Dedicated non-root user. UID 1001 (the gateway uses 1000) so the host's ./data can be owned by
# the same id: `sudo chown 1001:1001 data`.
RUN groupadd --system --gid 1001 psr \
    && useradd --system --uid 1001 --gid psr --home-dir /app --shell /usr/sbin/nologin psr

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install . \
    && mkdir -p /app/data \
    && chown psr:psr /app/data \
    && chmod 0700 /app/data

USER psr

# `docker compose run --rm notion-reports snapshot --config /app/config.yaml` runs one by hand.
ENTRYPOINT ["python", "-m", "t6_notion_reports"]
CMD ["serve", "--config", "/app/config.yaml"]
