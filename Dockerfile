# Safco Catalog Agent — production-ready container.
# Uses Playwright's official base image so Chromium + system deps are pre-installed.
FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependency layer first for Docker cache efficiency
COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install -e .

# App layer
COPY src ./src
COPY config ./config

# Non-root user for runtime safety
RUN useradd -m -u 10001 safco && chown -R safco:safco /app
USER safco

# Persistent volumes for data + logs
VOLUME ["/app/data", "/app/logs", "/app/debug"]

ENTRYPOINT ["safco"]
CMD ["crawl"]
