# syntax=docker/dockerfile:1
# Harborline 3PL Simulator image. Build context = the package root (docker-compose.yml sets it):
#   docker compose build app        or        docker build -f simulator/Dockerfile .
# Stage 1 builds the React UI (web/ + brand/ tokens); stage 2 runs the FastAPI app on :8080.

FROM node:22-alpine AS web
WORKDIR /src
COPY web/ /src/web/
COPY brand/ /src/brand/
RUN set -eu; \
    mkdir -p /src/web/dist; \
    if [ -f /src/web/package.json ]; then \
      cd /src/web; \
      if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi; \
      npm run build; \
    else \
      echo "web/package.json not found: the app will serve a placeholder page instead of the UI"; \
    fi

FROM python:3.12-slim AS app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp \
    HL_WEB_DIST=/app/web_dist \
    HL_LANDING_DIR=/data/landing \
    HL_CONFIG_DIR=/data/config \
    HL_TRACKER_DIR=/data/tracker
RUN groupadd --gid 10001 harborline \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin harborline
WORKDIR /app/simulator

# Dependencies first (cached until pyproject.toml / constraints.txt change), pinned to the tested versions.
COPY simulator/pyproject.toml simulator/constraints.txt ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))" \
        > /tmp/requirements.txt \
    && pip install -r /tmp/requirements.txt -c constraints.txt

COPY simulator/ ./
RUN pip install --no-deps -e . \
    && sed -i 's/\r$//' entrypoint.sh \
    && chmod +x entrypoint.sh \
    && mkdir -p /data/landing /data/answer_key \
    && chown -R harborline:harborline /data
COPY --from=web /src/web/dist /app/web_dist

USER harborline
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --start-period=90s --retries=5 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4).status == 200 else 1)"
ENTRYPOINT ["/app/simulator/entrypoint.sh"]
CMD ["uvicorn", "harborline.api.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--timeout-graceful-shutdown", "25"]
