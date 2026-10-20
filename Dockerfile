# syntax=docker/dockerfile:1
#
# Website test pipeline: the web app (FastAPI) plus the pipeline it launches for each run, with Chromium.
#
# The base image's tag MUST match the `playwright` pin in requirements-prod.txt: the image ships the browsers that
# exact version drives. Change both together.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

# the git commit this image was built from, shown on every run (docker compose passes it; see DOCKER.md)
ARG CODE_VERSION=unknown
ENV CODE_VERSION=${CODE_VERSION} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    HEADLESS=true

# Proves you may create the FIRST account. A default, so the app starts with no setup (safe while the port is bound to this
# machine only); set your own in docker.env before sharing it. It opens nothing once an account exists.
ENV ACCESS_CODE=change-me

WORKDIR /app

# dependencies first, so a change to the code does not reinstall them
COPY requirements-prod.txt ./
RUN python3 -m pip install -r requirements-prod.txt

# only what the app needs (see .dockerignore: everything else, .env above all, stays out of the image)
COPY api ./api
COPY website_test_pipeline ./website_test_pipeline
COPY static ./static
COPY conftest.py pyproject.toml AI-TEST-GUIDE.md persona.txt ./

# the database (app.db), the key file for saved API keys, and each run's scratch folder live here: mount a volume on it
RUN mkdir -p /data && chown -R pwuser:pwuser /data /app
USER pwuser
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python3", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"]

CMD ["python3", "-m", "uvicorn", "--factory", "api.main:create_app", "--host", "0.0.0.0", "--port", "8000"]
