#!/bin/sh
# Container start: migrate, collect static, create the first operator, serve.
# Every step is idempotent so restarts and redeploys are safe.
set -eu

echo "==> Applying migrations"
python manage.py migrate --noinput

echo "==> Collecting static files"
python manage.py collectstatic --noinput --clear

echo "==> Ensuring operator account and ISP"
python manage.py bootstrap

if [ "${SEED_DEMO:-false}" = "true" ]; then
  echo "==> Seeding demo data"
  python manage.py seed_demo || true
fi

echo "==> Starting gunicorn on :${PORT:-8000} (workers=${WEB_CONCURRENCY:-2})"
exec gunicorn config.wsgi:application \
  --bind "0.0.0.0:${PORT:-8000}" \
  --workers "${WEB_CONCURRENCY:-2}" \
  --threads "${WEB_THREADS:-4}" \
  --timeout 60 \
  --graceful-timeout 20 \
  --max-requests 1000 \
  --max-requests-jitter 100 \
  --access-logfile - \
  --error-logfile -
