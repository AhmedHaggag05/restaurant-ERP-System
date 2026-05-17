#!/usr/bin/env bash
# exit on error
set -o errexit

# Validate required secrets before doing anything
if [ -z "$DJANGO_SECRET_KEY" ]; then
  echo "ERROR: DJANGO_SECRET_KEY is not set. Aborting build."
  exit 1
fi
if [ -z "$DB_PASSWORD" ]; then
  echo "ERROR: DB_PASSWORD is not set. Aborting build."
  exit 1
fi

pip install -r requirements.txt
python manage.py collectstatic --no-input
python manage.py migrate
python manage.py createsuperuser --noinput || true