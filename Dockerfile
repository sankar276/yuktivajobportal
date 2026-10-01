FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    JOBPORTAL_DATA_DIR=/data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY config ./config
COPY src ./src

# Chromium renders the resume PDFs and reads application forms.
RUN pip install ".[llm]" \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R a+rX /opt/playwright

RUN useradd --create-home --uid 10001 jobportal \
    && mkdir -p /data \
    && chown jobportal:jobportal /data
USER jobportal
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"

# Listening on 0.0.0.0 requires JOBPORTAL_PASSWORD; the app refuses to start without it.
CMD ["jobportal", "serve", "--host", "0.0.0.0"]
