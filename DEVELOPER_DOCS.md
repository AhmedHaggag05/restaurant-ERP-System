# Searing Sandwiches — ERP Developer Documentation

> **Stack:** Python 3.14 · Django 5.2.7 · MySQL · ReportLab · openpyxl  
> **Last updated:** 2025

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Repository Layout](#2-repository-layout)
3. [Setup & Running Locally](#3-setup--running-locally)
4. [Django Apps & Responsibilities](#4-django-apps--responsibilities)
5. [Database Models Reference](#5-database-models-reference)
6. [URL Structure](#6-url-structure)
7. [Authentication & Permissions](#7-authentication--permissions)
8. [Reports System](#8-reports-system)
9. [Purchase Order Flow](#9-purchase-order-flow)
10. [Accounting Hooks](#10-accounting-hooks)
11. [Key Shared Utilities](#11-key-shared-utilities)
12. [Frontend / Templates](#12-frontend--templates)
13. [Adding a New Feature — Step-by-Step](#13-adding-a-new-feature--step-by-step)
14. [Common Pitfalls & Known Quirks](#14-common-pitfalls--known-quirks)
15. [Environment Variables](#15-environment-variables)

---

## 1. Project Overview

This is a **full-stack restaurant POS + ERP** built with Django. It handles:

- Point-of-sale (order taking, discounts, voids)
- Inventory management (ingredients, recipes, stock deduction on sale)
- Purchasing (suppliers, purchase orders, stock receiving)
- Human resources (employees, attendance, payroll)
- Accounting (journal entries, P&L, balance sheet)
- Reports (PDF + Excel exports across every module)

The "business logic" that ties modules together (e.g., a sale deducts stock AND posts a journal entry) lives in `core/views.py` wrapped in `try/except` so a bug in one module never crashes an order.

---

## 2. Repository Layout

```
pos_project/
│
├── config/                  # Django project settings & root URLconf
│   ├── settings.py
│   └── urls.py
│
├── core/                    # Main app — POS, menu, ingredients, orders, expenses
│   ├── models.py            # All core models (see §5)
│   ├── views.py             # ~1700 lines — POS, reports, PDF/Excel exports
│   ├── urls.py
│   └── templates/core/
│
├── hr/                      # Human resources
│   ├── models.py            # AttendanceRecord, SalaryRun, SalaryLine, Advance
│   ├── views.py
│   ├── urls.py
│   └── templates/hr/
│
├── purchasing/              # Procurement
│   ├── models.py            # Supplier, PurchaseOrder, PurchaseOrderLine, SupplierInvoice
│   ├── views.py
│   ├── urls.py
│   └── templates/purchasing/
│
├── accounting/              # Double-entry accounting
│   ├── models.py            # Account, JournalEntry, JournalLine
│   ├── utils.py             # Journal posting helpers (called from core/views.py)
│   ├── views.py
│   ├── urls.py
│   └── templates/accounting/
│
├── reports/                 # Unified download/export module
│   ├── views.py             # All PDF + Excel generators
│   ├── urls.py
│   └── templates/reports/
│       ├── hub.html             — Reports landing page
│       ├── finance_general.html — Full P&L HTML view
│       └── finance_custom.html  — Filterable custom report
│
├── templates/
│   └── erp_base.html        # Base layout — sidebar, topbar, CSS variables
│
├── static/                  # CSS / JS / images (served by Django in DEBUG)
├── requirements.txt
└── manage.py
```

---

## 3. Setup & Running Locally

### Prerequisites

- Python 3.12+
- MySQL server running locally
- pip

### Steps

```bash
# 1. Clone / extract the project
cd pos_project

# 2. Install Python deps
pip install -r requirements.txt
pip install openpyxl   # not in requirements.txt yet — add it

# 3. Configure database
# Create a .env file (or edit config/settings.py directly):
DB_NAME=your_db
DB_USER=your_user
DB_PASSWORD=your_pass
DB_HOST=localhost
DB_PORT=3306

# 4. Run migrations  ← always do this after pulling new code
python manage.py migrate

# 5. Create a superuser
python manage.py createsuperuser

# 6. Start the dev server
python manage.py runserver
```

> **Note:** After creating a superuser via `createsuperuser`, log in at `/erp/`  
> and manually set the user's **role** to `superadmin` via the Django admin at `/admin/`.

### requirements.txt (complete)

```
Django==5.2.7
mysqlclient==2.2.7
python-dotenv==1.1.1
reportlab==4.4.10
openpyxl>=3.1
```

---

## 4. Django Apps & Responsibilities

| App | Prefix | Purpose |
|-----|--------|---------|
| `core` | `/` | POS terminal, menu, ingredients, orders, expenses, users, promos, reports (legacy) |
| `hr` | `/hr/` | Employees, attendance, salary runs, advances |
| `purchasing` | `/purchasing/` | Suppliers, purchase orders, invoices, stock receiving |
| `accounting` | `/accounting/` | Chart of accounts, journal, P&L, balance sheet |
| `reports` | `/reports/` | Unified PDF/Excel export hub |

---

## 5. Database Models Reference

### core.Ingredient
| Field | Type | Notes |
|-------|------|-------|
| `name` | CharField(100) | Unique |
| `stock_quantity` | Decimal(12,3) | Always in the base unit (kg, L, pieces, boxes) |
| `unit` | CharField choices: kg / L / pieces / boxes | Base stock unit |
| `cost_per_unit` | Decimal(12,4) | EGP per base unit |
| `low_stock_threshold` | Decimal(10,3) | Triggers low-stock alert |
| `units_per_box` | PositiveInt | Boxes only |
| `sub_unit` | CharField | Boxes only (cans/packets) |

**Stock is NEVER manually edited.** It changes only via:
- Sale → `core.views.complete_order()` deducts stock via `StockLog`
- Purchase receiving → `purchasing.views.receive_po()` adds stock
- Manual adjustment in the admin

---

### core.Order / core.OrderItem
| Field | Notes |
|-------|-------|
| `order_number` | 8-char UUID hex, auto-generated |
| `status` | `active` or `voided` |
| `total_price` | Stored including any discount |
| `discount_amount` | EGP value of the promo discount |
| `promo_code` | FK → PromoCode (nullable) |

`OrderItem.price_at_order` stores the **sale price at the moment of sale**, not the current menu price. This ensures historical reports are accurate even if prices change later.

---

### core.StockLog
Every stock change writes a `StockLog` row:
| Field | Notes |
|-------|-------|
| `reason` | `sale` / `restock` / `manual` / `void` |
| `qty_before` | Stock before change |
| `qty_change` | Positive = gain, Negative = deduction |
| `qty_after` | Stock after change |

---

### core.ActivityLog
Audit trail for manager actions. Written by the `_log(request, action, target, detail)` helper in `core/views.py`.

---

### hr.AttendanceRecord
One row per employee per date. `unique_together = ('employee', 'date')`.

**Important:** Django `TimeField` renders as `"9:07 a.m."` in templates but expects `"HH:MM"` on save. Always use `|time:"H:i"` filter in templates and the `normalize_time()` helper in `hr/views.py` when reading POST data.

---

### purchasing.PurchaseOrder
| Status | Meaning |
|--------|---------|
| `draft` | Created, not yet sent |
| `sent` | Submitted to supplier |
| `partial` | Some lines received |
| `received` | Fully received — stock has been updated |
| `cancelled` | Abandoned |

`PurchaseOrderLine.total_cost` is auto-computed in `save()` as `quantity_ordered × unit_cost`.

**Unit convention:** All quantities in `PurchaseOrderLine` are **always stored in the ingredient's base stock unit** (kg, L, pieces, boxes). The UI allows the user to enter in grams/mL for convenience — JavaScript converts before submission.

---

### accounting.Account
Default chart of accounts (seeded by migration `0002_seed_coa`):
| Code | Name |
|------|------|
| 1000 | Cash / Bank |
| 1100 | Accounts Receivable |
| 1200 | Inventory |
| 2000 | Accounts Payable |
| 3000 | Retained Earnings |
| 4000 | Sales Revenue |
| 5000 | COGS |
| 5100 | Payroll Expense |
| 5200 | Operating Expenses |
| 5300 | Employee Advances |

---

## 6. URL Structure

### Root (`/`)
Handled by `core/urls.py`. Notable named URLs:
- `dashboard` → `/`
- `pos` → `/pos/`
- `sales_report` → `/sales-report/`
- `manage_ingredients_page` → `/ingredients/`
- `manage_menu` → `/menu/`
- `activity_log` → `/activity-log/`

### `/hr/`
- `hr:employees`, `hr:attendance`, `hr:salary_runs`, `hr:advances`

### `/purchasing/`
- `purchasing:suppliers`, `purchasing:purchase_orders`, `purchasing:create_po`
- `purchasing:po_detail` → `/purchasing/purchase-orders/<pk>/`
- `purchasing:invoices`

### `/reports/`
- `reports:hub` → landing page
- `reports:finance_general` → HTML P&L view
- `reports:finance_custom` → filterable custom report
- `reports:finance_general_pdf` / `reports:finance_general_excel`
- `reports:finance_custom_pdf` / `reports:finance_custom_excel`
- `reports:purchasing_pdf` / `reports:purchasing_excel`
- `reports:hr_attendance_pdf` / `reports:hr_attendance_excel`
- `reports:hr_payroll_pdf` / `reports:hr_payroll_excel`
- `reports:inventory_pdf` / `reports:inventory_excel`
- `reports:activity_pdf` / `reports:activity_excel`

All export URLs accept `?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD`.

### `/accounting/`
- `accounting:chart_of_accounts`, `accounting:journal`
- `accounting:profit_loss`, `accounting:balance_sheet`

---

## 7. Authentication & Permissions

### User roles (stored in `core.UserProfile.role`)
| Role | Access |
|------|--------|
| `superadmin` | Everything |
| `manager` | ERP + all reports |
| `cashier` | POS only |

### Decorators
```python
# Requires manager OR superadmin
from core.views import manager_required

@manager_required
def my_view(request):
    ...
```

```python
# Requires any authenticated user (for POS)
from django.contrib.auth.decorators import login_required
```

`manager_required` is defined in `core/views.py`. It checks `request.user.profile.role in ('manager', 'superadmin')`.

---

## 8. Reports System

All reports live in **`reports/views.py`**. The architecture:

### PDF generation (ReportLab)
```python
# Typical pattern
from io import BytesIO
buf  = BytesIO()
doc  = _pdf_doc(buf, land=True)   # land=True for landscape A4
story = []
_pdf_header(story, 'Report Title', 'Subtitle', 'Period string')
# ... build table rows ...
t = Table(rows, colWidths=[...], repeatRows=1)
t.setStyle(_base_ts())
story.append(t)
doc.build(story)
return _pdf_response(buf, 'filename.pdf')
```

### Excel generation (openpyxl)
```python
wb = Workbook()
ws = wb.active; ws.title = 'Sheet Name'
_xl_title(ws, 'Report Title')
_xl_headers(ws, [('Column A', 20), ('Column B', 16)], row=2)
for r, obj in enumerate(queryset, 3):
    _xl_cell(ws, r, 1, obj.name)
    _xl_cell(ws, r, 2, float(obj.amount), num_fmt='#,##0.00', align=_AL_R)
_xl_total_row(ws, len(queryset)+3, ['TOTAL', total_amount])
return _xl_response(wb, 'filename.xlsx')
```

### Shared style helpers in `reports/views.py`

| Helper | Purpose |
|--------|---------|
| `_pdf_doc(buf, land=False)` | Create A4 SimpleDocTemplate |
| `_pdf_header(story, title, subtitle, period)` | Add orange-branded header to PDF |
| `_base_ts()` | TableStyle with orange header, alternating rows |
| `_totals_row_style(ts, row_idx)` | Add totals-row highlight |
| `_pdf_response(buf, filename)` | Return HttpResponse as PDF download |
| `_xl_title(ws, text)` | Write orange title cell to worksheet |
| `_xl_headers(ws, cols, row)` | Write styled header row |
| `_xl_cell(ws, row, col, value, ...)` | Write a data cell with border + font |
| `_xl_total_row(ws, row, values)` | Write highlighted totals row |
| `_xl_response(wb, filename)` | Return HttpResponse as Excel download |
| `_get_dates(request)` | Extract `date_from`/`date_to` from GET or POST |
| `_filter_by_date(qs, df, dt, field)` | Apply date range filter to queryset |
| `_period_label(df, dt)` | Human-readable period string |
| `_convert_to_stock_unit(qty, recipe_unit, stock_unit)` | Unit conversion for COGS calc |

### Finance data engines

```python
# General report — all KPIs
data = _build_finance_general(date_from_str, date_to_str)
# Returns: n_orders, total_rev, total_cogs, gross_profit, gross_margin,
#          total_exp, payroll, net_profit, avg_order, items[], expenses_qs

# Custom grouped report
data = _build_finance_custom(request.GET)
# group_by options: 'item' | 'category' | 'day' | 'week' | 'month'
# Returns: rows[], group_by, period, total_rev, total_cogs, total_gp
```

### Adding a new report

1. Add two view functions to `reports/views.py`:
   ```python
   @manager_required
   def my_thing_pdf(request):
       df, dt = _get_dates(request)
       qs = MyModel.objects.all()
       qs = _filter_by_date(qs, df, dt, field='created_at__date')
       # ... build PDF ...
       return _pdf_response(buf, 'my_thing.pdf')

   @manager_required
   def my_thing_excel(request):
       # ... build Excel ...
       return _xl_response(wb, 'my_thing.xlsx')
   ```

2. Add two URL patterns to `reports/urls.py`:
   ```python
   path('my-thing/pdf/',   views.my_thing_pdf,   name='my_thing_pdf'),
   path('my-thing/excel/', views.my_thing_excel,  name='my_thing_excel'),
   ```

3. Add a card to `reports/templates/reports/hub.html`.

---

## 9. Purchase Order Flow

```
Create PO (draft)
    ↓
[Optional] Mark as Sent
    ↓
Receive PO (full or partial) → purchasing/views.py receive_po()
    ↓  
    • PurchaseOrderLine.quantity_received += received_qty
    • Ingredient.stock_quantity += received_qty
    • StockLog row written (reason='restock')
    • CostHistory row written (old_cost → new_cost)
    • Ingredient.cost_per_unit updated to new unit_cost
    • accounting/utils.post_purchase_journal() called
    • PO status → 'received' or 'partial'
```

### Unit entry on the PO form

The Create PO form (`purchasing/templates/purchasing/create_po.html`) has JavaScript that:
- Shows a **kg / g** toggle for ingredients with `unit='kg'`
- Shows an **L / mL** toggle for `unit='L'`
- Lets the user enter either **Total Paid** or **Cost Per Unit**
- Converts everything to the base unit before writing hidden `<input name="quantity">` and `<input name="unit_cost">` fields
- The server reads only the hidden fields — so no conversion logic is needed in the view

---

## 10. Accounting Hooks

Located in `accounting/utils.py`. Called non-blockingly from `core/views.py`:

```python
try:
    from accounting.utils import post_sale_journal
    post_sale_journal(order)
except Exception:
    pass  # never crash a sale due to accounting bug
```

| Function | Triggered by | Journal entry |
|----------|-------------|---------------|
| `post_sale_journal(order)` | Order completion | Dr Cash · Cr Revenue; Dr COGS · Cr Inventory |
| `post_purchase_journal(po)` | PO received | Dr Inventory · Cr AP |
| `post_payroll_journal(salary_run)` | Payroll run paid | Dr Payroll Expense · Cr Cash |
| `post_expense_journal(expense)` | Expense logged | Dr Operating Expenses · Cr Cash |
| `post_void_journal(order)` | Order voided | Reversal of sale entry |

---

## 11. Key Shared Utilities

### `core/views.py` — helper functions used widely

```python
_log(request, action, target='', detail='')
# Writes an ActivityLog row. action must be in ActivityLog.ACTION_CHOICES.

_convert_to_stock_unit(qty, recipe_unit, stock_unit) -> float
# Converts recipe quantities to stock unit for COGS calculation.
# e.g. _convert_to_stock_unit(200, 'g', 'kg') → 0.2

manager_required  # decorator — redirects non-managers to login
```

### `hr/views.py` — attendance time normalisation

```python
normalize_time(value: str) -> str | None
# Converts "9:07 a.m." → "09:07", "2:30 p.m." → "14:30"
# Returns None for unparseable values
# MUST be applied to any check_in / check_out POST data before saving
```

---

## 12. Frontend / Templates

### Base layout: `templates/erp_base.html`

All ERP pages extend this. It provides:
- Left sidebar (240 px, dark `#1a1d23`) with collapsible mobile menu
- Top bar with username and role
- CSS variables: `--primary: #FF4500`, `--sidebar-w: 240px`
- Bootstrap 5 + Bootstrap Icons loaded from CDN

### Key CSS classes (defined in `erp_base.html`)

| Class | Purpose |
|-------|---------|
| `.erp-card` | White card with shadow |
| `.erp-card-header` | Orange-bordered section heading |
| `.erp-input` | Styled form input |
| `.erp-form-label` | Small uppercase label |
| `.erp-table` | Striped data table |
| `.erp-page-header` | Flex row with title + action buttons |
| `.erp-page-title` | H1 style |
| `.erp-page-sub` | Subtitle text |
| `.btn-primary-erp` | Orange primary button |
| `.btn-erp` | Base button (padding/radius) |
| `.sidebar-link` | Sidebar navigation item |
| `.sidebar-section-label` | Sidebar section heading |

### Template blocks

```html
{% extends "erp_base.html" %}
{% block title %}Page Title{% endblock %}
{% block page_title %}Topbar Title{% endblock %}
{% block content %}...{% endblock %}
{% block extra_js %}...{% endblock %}
```

### Adding a page to the sidebar

Edit `templates/erp_base.html` and add a link inside the relevant `<div class="sidebar-section">`:

```html
<a href="{% url 'myapp:myview' %}"
   class="sidebar-link {% if 'myview' in request.resolver_match.url_name %}active{% endif %}">
    <i class="bi bi-icon-name"></i> Label
</a>
```

---

## 13. Adding a New Feature — Step-by-Step

### Example: Add a "Loyalty Points" module

**Step 1 — Create the app**
```bash
python manage.py startapp loyalty
```

**Step 2 — Register in `config/settings.py`**
```python
INSTALLED_APPS = [
    ...
    'loyalty',
]
```

**Step 3 — Write models (`loyalty/models.py`)**
```python
from django.db import models
from core.models import Order

class LoyaltyAccount(models.Model):
    customer_phone = models.CharField(max_length=20, unique=True)
    points         = models.PositiveIntegerField(default=0)
    created_at     = models.DateTimeField(auto_now_add=True)
```

**Step 4 — Create and run migrations**
```bash
python manage.py makemigrations loyalty
python manage.py migrate
```

**Step 5 — Write views (`loyalty/views.py`)**
- Import `manager_required` from `core.views`
- Decorate all views with `@manager_required`

**Step 6 — Wire URLs**
```python
# loyalty/urls.py
from django.urls import path
from . import views
app_name = 'loyalty'
urlpatterns = [
    path('', views.loyalty_dashboard, name='dashboard'),
]

# config/urls.py — add:
path('loyalty/', include('loyalty.urls')),
```

**Step 7 — Create templates**
```
loyalty/templates/loyalty/dashboard.html
```
Extend `erp_base.html`.

**Step 8 — Add to sidebar**  
Edit `templates/erp_base.html` — see §12.

**Step 9 — Add report if needed**  
Add `*_pdf` and `*_excel` views to `reports/views.py` and URLs to `reports/urls.py`.

---

## 14. Common Pitfalls & Known Quirks

### Django TimeField format mismatch
`TimeField` values render as `"9:07 a.m."` in Django templates. If you carry these into hidden form fields and POST them back, the model save will raise `ValidationError`. **Always** use `{{ rec.check_in|time:"H:i" }}` in templates and call `normalize_time()` in views before saving attendance data.

### Stock quantity precision
`stock_quantity` has 3 decimal places. Recipe quantities entered as grams are divided by 1000 before storage. If you see off-by-one stock discrepancies, check that all callers of `_convert_to_stock_unit()` use `float()` not `Decimal()` for the conversion factor.

### Accounting hooks must not break sales
All calls to `accounting/utils.py` are wrapped in `try/except Exception: pass`. Do NOT remove this pattern. A bug in accounting must never prevent a cashier from completing a sale.

### `openpyxl` not in requirements.txt
The `reports` app uses `openpyxl`. It was installed manually. Add to `requirements.txt`:
```
openpyxl>=3.1
```

### PO unit convention
`PurchaseOrderLine.quantity_ordered` is **always in the ingredient's base stock unit** regardless of what the user typed in the form. The JS converts grams → kg before submission. Never trust the raw UI input — always read from the hidden fields.

### Cost-per-unit updates on PO receive
When a PO is received, `Ingredient.cost_per_unit` is updated to the latest purchase unit cost. This affects future COGS calculations but does NOT retroactively change historical order costs (those are captured at sale time in `OrderItem.price_at_order` and COGS is recalculated from current `cost_per_unit` at report time).

### MySQL strict mode
If you see `django.db.utils.OperationalError: (1292, "Incorrect datetime value")`, your MySQL server has `STRICT_TRANS_TABLES` enabled. Make sure all DateTimeField defaults use `timezone.now` not `datetime.now`.

---

## 15. Environment Variables

The project uses `python-dotenv`. Create a `.env` file in the project root:

```
SECRET_KEY=your-secret-key-here
DEBUG=True

DB_NAME=searing_db
DB_USER=root
DB_PASSWORD=yourpassword
DB_HOST=localhost
DB_PORT=3306
```

`config/settings.py` reads these with:
```python
from dotenv import load_dotenv
load_dotenv()
import os
SECRET_KEY = os.environ.get('SECRET_KEY')
```

> **For production:** Set `DEBUG=False`, configure a proper `ALLOWED_HOSTS`, use a strong `SECRET_KEY`, and serve static files via nginx/Apache rather than Django.

---

*End of documentation. For questions about specific business logic, read the docstrings in `core/views.py` and `reports/views.py` — most functions have a short description of what they do and what they return.*
