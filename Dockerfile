# syntax=docker/dockerfile:1
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ANYACLAW_STORAGE__DATA_DIR=/data \
    ANYACLAW_SECURITY__WORKSPACE=/data/workspace \
    ANYACLAW_WEB__HOST=0.0.0.0 \
    ANYACLAW_WEB__PORT=8080

WORKDIR /app

# Install dependencies first for better layer caching.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install ".[web,office]" \
 && useradd --create-home --uid 10001 anyaclaw \
 && mkdir -p /data/workspace /data/files \
 && chown -R anyaclaw:anyaclaw /data /app

USER anyaclaw

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3).status == 200 else 1)"

ENTRYPOINT ["anyaclaw"]
CMD ["serve", "--no-cli"]
