# Flatlogic Python Template Workspace

This workspace houses the Django application scaffold used for Python-based templates.

## Requirements

- Python 3.11+
- MariaDB (or MySQL-compatible server) with the credentials prepared by `setup_mariadb_project.sh`
- System packages: `pkg-config`, `libmariadb-dev` (already installed on golden images)

## Getting Started

```bash
python3 -m pip install --break-system-packages -r requirements.txt
python3 manage.py migrate
python3 manage.py runserver 0.0.0.0:8000
```

Environment variables are loaded from `../.env` (the executor root). See `.env.example` if you need to populate values manually.

## Project Structure

- `config/` – Django project settings, URLs, WSGI entrypoint.
- `core/` – Default app with a basic health-check route.
- `manage.py` – Django management entrypoint.

## Next Steps

- Create additional apps and views according to the generated project requirements.
- Configure serving via Apache + mod_wsgi or gunicorn (instructions to be added).
- Run `python3 manage.py collectstatic` before serving through Apache.
