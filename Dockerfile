# Single image, single service. Static files are served by WhiteNoise, so no
# nginx sidecar and no second container to keep healthy.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN chmod +x entrypoint.sh \
    && mkdir -p /app/data /app/staticfiles \
    && chown -R app:app /app

USER app

ENV DJANGO_SETTINGS_MODULE=config.settings \
    PORT=8000 \
    WEB_CONCURRENCY=2

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]
