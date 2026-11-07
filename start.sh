#!/bin/sh
set -e

export PYTHONPATH=/app

echo "Running database migrations..."
alembic upgrade head

echo "Starting server..."
exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}" --workers "${UVICORN_WORKERS:-1}" --timeout-keep-alive 15 --no-access-log
