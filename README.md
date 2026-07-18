# 🍔 Searing Sandwiches - Enterprise ERP & POS Backend

A comprehensive, custom-built Enterprise Resource Planning (ERP) and Point of Sale (POS) backend designed specifically for high-volume restaurant operations. 

Engineered from the ground up to handle live kitchen syncing, granular role-based access control, and seamless multi-client support, this system powers both an administrative web portal and a cross-platform mobile application.

## 🏗️ Architecture Overview

The backend is built as a robust **Django 5.2.7** monolith backed by **TiDB (MySQL)**. To support future scalability and multi-platform clients without compromising the legacy web portal, the architecture implements a strict boundary between web and mobile clients:

* **Administrative Web Portal:** Utilizes traditional server-side rendering and stateful Session Authentication for secure, internal management.
* **Isolated Mobile API (`/api/mobile/`):** A dedicated, stateless REST API built with Django REST Framework (DRF), secured via JWT, and optimized for mobile client consumption (React Native/Expo).

This project was developed leveraging modern AI tooling to accelerate boilerplate generation, optimize database queries, and enforce secure architectural patterns.

## 🚀 Core Features

* **Dual Authentication Pipeline:** Safely maintains Session Auth for the web portal while exposing a robust `djangorestframework-simplejwt` pipeline for mobile endpoints.
* **Role-Based Access Control (RBAC):** Strict permission classes isolating endpoints between Cashiers (order entry, menu read) and Managers (financial KPIs, system overrides).
* **Real-Time Kitchen Display System (KDS):** Endpoints optimized to serve active, live orders directly to kitchen staff displays.
* **Smart Inventory Management:** Transactional order creation that automatically validates ingredient stock and blocks orders (`400 Bad Request`) upon insufficient inventory.
* **Enterprise Security:** Comprehensive API rate-limiting, CORS configuration, and endpoint isolation to prevent unauthorized data access.

## 💻 Tech Stack

* **Framework:** Django 5.2.7
* **API Layer:** Django REST Framework (DRF)
* **Database:** TiDB / MySQL
* **Authentication:** SimpleJWT (Stateless) & Django Session Auth (Stateful)
* **Security:** Django-Ratelimit, custom DRF Permission Classes

## 📡 Mobile API Contract Reference

The isolated mobile API serves the React Native POS application. Below are the core endpoints:

**Authentication**
* `POST /api/mobile/login/` - Issues `{access, refresh}` tokens.
* `POST /api/mobile/refresh/` - Issues new access token from valid refresh token.
* `GET /api/mobile/me/` - Returns active user profile `{id, username, role}`.

**Operations**
* `GET /api/mobile/menu/` - Retrieves categorized menu items and pricing.
* `POST /api/mobile/create-order/` - Processes cart payload. Validates stock, applies promo codes, and assigns table numbers.
* `GET /api/mobile/live-orders/` - Retrieves active queue for the Kitchen Display.

**Management**
* `GET /api/mobile/dashboard-kpis/` - Retrieves live financial data (Requires Manager Role).

## 🛠️ Local Development Setup

**1. Clone the repository**
```bash
git clone https://github.com/yourusername/searing-sandwiches-backend.git
cd searing-sandwiches-backend
```

**2. Set up the virtual environment**
```bash
python -m venv venv
source venv/bin/activate  # On Windows use `venv\Scripts\activate`
```

**3. Install dependencies**
```bash
pip install -r requirements.txt
```

**4. Configure Environment Variables**
Create a `.env` file in the root directory and add your TiDB credentials and secret keys:
```env
DEBUG=True
SECRET_KEY=your_secret_key
DB_NAME=searing_db
DB_USER=root
DB_PASSWORD=your_password
DB_HOST=127.0.0.1
DB_PORT=4000
```

**5. Run Migrations and Start Server**
```bash
python manage.py migrate
python manage.py runserver 0.0.0.0:8000
```
