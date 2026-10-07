# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Сначала зависимости точных версий из requirements.lock — слой кешируется
COPY pyproject.toml requirements.lock ./
RUN pip install --upgrade pip \
    && pip install -r requirements.lock \
    && mkdir app && touch app/__init__.py && pip install --no-deps . && rm -rf app

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && useradd --create-home --uid 1000 appuser
USER appuser

ENV APP_ENV=production \
    COOKIE_SECURE=true \
    PORT=8000 \
    WEB_CONCURRENCY=2

EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
