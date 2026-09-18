"""Isolated SQLite settings for the project's regression tests."""

import os

os.environ.setdefault('DJANGO_SECRET_KEY', 'tests-only-not-for-deployment')
os.environ['DJANGO_DEBUG'] = 'true'

from .settings import *  # noqa: F403,E402

DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}
CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
SECURE_SSL_REDIRECT = False
