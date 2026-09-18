# Searing Sandwiches ERP & POS

A Django web application for restaurant point of sale and back-office operations. It includes order entry, menu and ingredient management, purchasing, employee attendance and payroll, accounting journals, and PDF/Excel reports.

## Architecture

- **Web UI:** Django server-rendered templates with session authentication and CSRF protection.
- **Business modules:** `core`, `purchasing`, `hr`, `accounting`, and `reports`.
- **Database:** TiDB through the `django-tidb` backend and MySQL protocol.
- **Access:** Cashiers can use the POS; managers can access operational and financial pages; super admins manage accounts.

The browser POS uses `POST /api/create-order/` for order submission and `POST /api/validate-promo/` for promo validation. These endpoints use the authenticated web session. There is no mobile API in this project.

## Local setup

1. Clone the repository and open its root directory:

   ```bash
   git clone https://github.com/AhmedHaggag05/restaurant-ERP-System.git
   cd restaurant-ERP-System
   ```

2. Create a Python virtual environment and install dependencies:

   ```bash
   python -m venv .venv
   # Windows: .venv\Scripts\activate
   # macOS/Linux: source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. Create `.env` in the repository root with your own development values:

   ```env
   DJANGO_SECRET_KEY=replace-with-a-long-random-development-key
   DJANGO_DEBUG=true
   DB_NAME=your_database
   DB_USER=your_database_user
   DB_PASSWORD=your_database_password
   DB_HOST=your_tidb_host
   DB_PORT=4000
   ALLOWED_HOSTS=localhost,127.0.0.1
   ```

   `config/settings.py` also loads `.env` from the parent directory. Keep credentials out of Git.

4. Apply migrations and create the database-backed cache table, then start the server:

   ```bash
   python manage.py migrate
   python manage.py createcachetable
   python manage.py runserver
   ```

## Production changes

`build.sh` installs dependencies, collects static files, runs migrations, and creates the cache table during a Render build. Review migrations and back up production data before merging a deployment branch. The application intentionally keeps voided orders in history; it does not permanently delete orders that have stock and accounting records.

Older orders created before the stock-consumption snapshot migration can be voided using their historical stock logs. Their item quantities cannot be edited automatically because a later recipe change makes the original per-item consumption impossible to reconstruct reliably.
