FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/matplotlib \
    STEPWISE_DATA_DIR=/var/lib/stepwise

WORKDIR /app

RUN groupadd --system stepwise \
    && useradd --system --gid stepwise --home-dir /nonexistent --shell /usr/sbin/nologin stepwise

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install --no-cache-dir --upgrade pip setuptools wheel \
    && python -m pip install --no-cache-dir . \
    && mkdir -p /var/lib/stepwise \
    && chown -R stepwise:stepwise /var/lib/stepwise

USER stepwise

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"]

CMD ["uvicorn", "stepwise.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
