#!/bin/sh
# Ждём базу, применяем миграции и запускаем Gunicorn с воркерами Uvicorn.
set -e
# база может подниматься одновременно с приложением (перезапуск на хостинге)
python -m app.cli wait-db --timeout "${DB_WAIT_SECONDS:-60}"
alembic upgrade head
# --forwarded-allow-ips: доверяем X-Forwarded-Proto от прокси хостинга (HTTPS терминируется там)
exec gunicorn app.main:app \
  --worker-class uvicorn.workers.UvicornWorker \
  --workers "${WEB_CONCURRENCY:-2}" \
  --bind "0.0.0.0:${PORT:-8000}" \
  --forwarded-allow-ips "*" \
  --access-logfile - \
  --timeout 60
