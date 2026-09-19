from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse
from django.db import transaction
from django.db.models import Sum, F, Count
from django.utils import timezone
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.decorators import user_passes_test
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.paginator import Paginator
from django.conf import settings as django_settings
from django.core.mail import send_mail

import json
import logging
from datetime import datetime, date
from decimal import Decimal, InvalidOperation

from .models import (
    Ingredient, MenuItem, MenuItemIngredient, Order, OrderItem,
    UserProfile, Employee, PromoCode, Expense, MenuCategory,
    StockLog, CostHistory, ActivityLog, DamagedStock, OrderItemConsumption,
)
from .financials import line_cogs, net_line_revenues

logger = logging.getLogger(__name__)


# ── Security helpers ───────────────────────────────────────────────────────────

def _get_client_ip(request):
    """Return the best-guess real client IP (Render uses X-Forwarded-For)."""
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '127.0.0.1')


def _is_rate_limited(request, key_prefix, max_attempts=None, window=None):
    """Return True if this IP has exceeded the attempt threshold."""
    if max_attempts is None:
        max_attempts = getattr(django_settings, 'LOGIN_RATE_LIMIT_ATTEMPTS', 5)
    if window is None:
        window = getattr(django_settings, 'LOGIN_RATE_LIMIT_WINDOW', 300)
    ip = _get_client_ip(request)
    cache_key = f'{key_prefix}:{ip}'
    attempts = cache.get(cache_key, 0)
    return attempts >= max_attempts


def _record_attempt(request, key_prefix, window=None):
    """Increment the failed-attempt counter for this IP."""
    if window is None:
        window = getattr(django_settings, 'LOGIN_RATE_LIMIT_WINDOW', 300)
    ip = _get_client_ip(request)
    cache_key = f'{key_prefix}:{ip}'
    try:
        cache.incr(cache_key)
    except ValueError:
        cache.set(cache_key, 1, window)


def _clear_attempts(request, key_prefix):
    """Clear the failed-attempt counter on successful auth."""
    ip = _get_client_ip(request)
    cache.delete(f'{key_prefix}:{ip}')


# ── Activity logging ───────────────────────────────────────────────────────────

def _log(request, action, target='', detail=''):
    """Write an ActivityLog row. Never raises — logging must not break views."""
    try:
        ip = _get_client_ip(request)
        ActivityLog.objects.create(
            user=request.user if request.user.is_authenticated else None,
            action=action,
            target=str(target)[:200],
            detail=str(detail)[:2000],
            ip_address=ip or None,
        )
    except Exception:
        pass


# ── Unit-conversion helpers ────────────────────────────────────────────────────

UNIT_FAMILIES = {
    'kg': ['g'],
    'L': ['mL'],
    'pieces': ['pieces'],
}


def _allowed_recipe_units(ingredient):
    if ingredient.unit == 'boxes':
        return [ingredient.sub_unit]
    return UNIT_FAMILIES.get(ingredient.unit, ['pieces'])


def _validate_recipe_unit(ingredient, recipe_unit):
    return recipe_unit in _allowed_recipe_units(ingredient)


def _convert_to_stock_unit(qty, recipe_unit, stock_unit):
    qty = Decimal(str(qty))
    if recipe_unit == 'g' and stock_unit == 'kg': return qty / Decimal('1000')
    if recipe_unit == 'mL' and stock_unit == 'L':  return qty / Decimal('1000')
    return qty


def _log_stock(ingredient, qty_change, reason, user=None, note=''):
    # Callers persist the new stock quantity before recording the change.
    before = ingredient.stock_quantity - qty_change
    return StockLog.objects.create(
        ingredient=ingredient,
        changed_by=user,
        reason=reason,
        qty_before=before,
        qty_change=qty_change,
        qty_after=before + qty_change,
        note=str(note)[:200],
    )


def _send_low_stock_alert(ingredient):
    import threading
    def _send():
        try:
            manager_emails = list(
                User.objects.filter(
                    profile__role__in=('manager', 'superadmin'),
                    email__isnull=False,
                ).exclude(email='').values_list('email', flat=True)
            )
            if not manager_emails:
                return
            subject = f"[Searing Sandwiches] Low Stock: {ingredient.name}"
            body = (
                f"Stock Alert\n\n"
                f"Ingredient: {ingredient.name}\n"
                f"Current Stock: {ingredient.stock_quantity} {ingredient.unit}\n"
                f"Threshold: {ingredient.low_stock_threshold} {ingredient.unit}\n\n"
                f"Please restock soon.\n\n— Searing Sandwiches POS"
            )
            send_mail(subject, body, django_settings.DEFAULT_FROM_EMAIL,
                      manager_emails, fail_silently=True)
        except Exception:
            pass

    threading.Thread(target=_send, daemon=True).start()


# ── Role helpers ───────────────────────────────────────────────────────────────

def is_superadmin(user):
    return (user.is_authenticated
            and hasattr(user, 'profile')
            and user.profile.role == 'superadmin')


def is_manager(user):
    return (user.is_authenticated
            and hasattr(user, 'profile')
            and user.profile.role in ('manager', 'superadmin'))


def is_cashier(user):
    return (user.is_authenticated
            and hasattr(user, 'profile')
            and user.profile.role == 'cashier')


def manager_required(view_func):
    return user_passes_test(is_manager, login_url='login')(view_func)


def superadmin_required(view_func):
    return user_passes_test(is_superadmin, login_url='login')(view_func)


def cashier_or_manager_required(view_func):
    return user_passes_test(
        lambda u: is_manager(u) or is_cashier(u), login_url='login'
    )(view_func)


# ── Error handlers ─────────────────────────────────────────────────────────────

def handler_400(request, exception=None):
    return render(request, 'errors/400.html', status=400)


def handler_403(request, exception=None):
    return render(request, 'errors/403.html', status=403)


def handler_404(request, exception=None):
    return render(request, 'errors/404.html', status=404)


def handler_500(request):
    return render(request, 'errors/500.html', status=500)


# ═══════════════════════════════════════════════════════════════════════════════
#  AUTH VIEWS
# ═══════════════════════════════════════════════════════════════════════════════

def login_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard' if is_manager(request.user) else 'pos')

    if request.method == 'POST':
        # Rate limiting before any DB query
        if _is_rate_limited(request, 'login'):
            return render(request, 'core/login.html', {
                'form': AuthenticationForm(),
                'rate_limited': True,
            })

        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            _clear_attempts(request, 'login')
            _log(request, 'login', user.username)
            return redirect('dashboard' if is_manager(user) else 'pos')
        else:
            _record_attempt(request, 'login')
            logger.warning(
                'Failed login attempt for username=%r ip=%s',
                request.POST.get('username', '')[:50],
                _get_client_ip(request),
            )
    else:
        form = AuthenticationForm()

    return render(request, 'core/login.html', {'form': form})


def logout_view(request):
    if request.user.is_authenticated:
        _log(request, 'logout', request.user.username)
    logout(request)
    return redirect('login')


def home(request):
    if request.user.is_authenticated:
        return redirect('dashboard' if is_manager(request.user) else 'pos')
    return render(request, "core/index.html")


# ═══════════════════════════════════════════════════════════════════════════════
#  POS VIEWS
# ═══════════════════════════════════════════════════════════════════════════════

@cashier_or_manager_required
def pos_view(request):
    menu_items = MenuItem.objects.filter(is_active=True).select_related('category')
    categories = MenuCategory.objects.all()
    return render(request, "core/pos.html", {
        "menu_items": menu_items,
        "categories": categories,
    })


@cashier_or_manager_required
def create_order(request):
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Method not allowed"}, status=405)

    content_type = request.content_type or ''
    if 'application/json' not in content_type:
        return JsonResponse({"success": False, "error": "Invalid content type"}, status=400)

    if len(request.body) > 512 * 1024:  # 512 KB max
        return JsonResponse({"success": False, "error": "Request too large"}, status=413)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    try:
        cart = data.get("cart", [])
        notes = str(data.get("notes", ""))[:500]
        table_number = str(data.get("table_number", "")).strip()[:10]
        promo_code_str = str(data.get("promo_code", "")).strip().upper()[:30]

        if not cart or not isinstance(cart, list):
            return JsonResponse({"success": False, "error": "Cart is empty"}, status=400)

        if len(cart) > 50:
            return JsonResponse({"success": False, "error": "Too many items in cart"}, status=400)

        # Validate cart items
        for item in cart:
            if not isinstance(item, dict):
                return JsonResponse({"success": False, "error": "Invalid cart item"}, status=400)
            try:
                qty = int(item.get("quantity", 0))
                if qty < 1 or qty > 99:
                    return JsonResponse({"success": False, "error": "Invalid item quantity"}, status=400)
                int(item["id"])
            except (TypeError, ValueError, KeyError):
                return JsonResponse({"success": False, "error": "Invalid cart item"}, status=400)

        with transaction.atomic():
            order = Order.objects.create(customer_notes=notes, table_number=table_number)
            subtotal = Decimal("0.00")

            # Lock the promo row (if any) for the lifetime of this transaction so two
            # concurrent orders can't both pass the max_uses check before either commits.
            promo_obj = None
            discount_amount = Decimal("0.00")
            if promo_code_str:
                try:
                    promo_obj = PromoCode.objects.select_for_update().get(
                        code__iexact=promo_code_str, is_active=True)
                except PromoCode.DoesNotExist:
                    raise ValueError("Promo code is invalid or inactive.")

            item_ids = [item["id"] for item in cart]
            menu_items_qs = MenuItem.objects.filter(id__in=item_ids).prefetch_related(
                'ingredients__ingredient'
            )
            menu_items_map = {mi.id: mi for mi in menu_items_qs}

            for item in cart:
                menu_item = menu_items_map.get(int(item["id"]))
                if not menu_item or not menu_item.is_active:
                    raise ValueError("Menu item not found")
                quantity = int(item["quantity"])
                cogs_unit = Decimal('0')
                consumed = {}
                for recipe_item in menu_item.ingredients.all():
                    recipe_qty = recipe_item.quantity_required * quantity
                    required_qty = _convert_to_stock_unit(
                        recipe_qty, recipe_item.recipe_unit, recipe_item.ingredient.unit
                    ).quantize(Decimal('0.001'))
                    if required_qty < 0:
                        raise ValueError('Recipe quantities must not be negative.')
                    if required_qty == 0:
                        continue
                    ingredient = Ingredient.objects.select_for_update().get(
                        id=recipe_item.ingredient.id)
                    cogs_unit += required_qty * ingredient.cost_per_unit
                    if ingredient.stock_quantity < required_qty:
                        raise ValueError(f"Insufficient stock for {ingredient.name}")
                    ingredient.stock_quantity -= required_qty
                    ingredient.save()
                    consumed[ingredient.id] = consumed.get(ingredient.id, Decimal('0')) + required_qty
                    _log_stock(ingredient, -required_qty, 'sale', request.user,
                               note=f'Order {order.order_number}')
                    if ingredient.stock_quantity <= ingredient.low_stock_threshold:
                        transaction.on_commit(lambda ing=ingredient: _send_low_stock_alert(ing))
                order_item = OrderItem.objects.create(
                    order=order, menu_item=menu_item,
                    quantity=quantity, price_at_order=menu_item.price,
                    cogs_at_sale=cogs_unit, stock_snapshot_ready=True,
                )
                OrderItemConsumption.objects.bulk_create([
                    OrderItemConsumption(order_item=order_item, ingredient_id=ing_id,
                                         quantity_consumed=consumed_qty)
                    for ing_id, consumed_qty in consumed.items()
                ])
                subtotal += menu_item.price * quantity

            if promo_obj:
                today = timezone.now().date()
                if promo_obj.valid_from and today < promo_obj.valid_from:
                    raise ValueError("Promo code is not yet valid.")
                if promo_obj.valid_until and today > promo_obj.valid_until:
                    raise ValueError("Promo code has expired.")
                if promo_obj.max_uses and promo_obj.times_used >= promo_obj.max_uses:
                    raise ValueError("Promo code has reached its usage limit.")
                if promo_obj.discount_type == 'percent':
                    discount_amount = (
                            subtotal * promo_obj.discount_value / Decimal("100")
                    ).quantize(Decimal("0.01"))
                else:
                    discount_amount = min(promo_obj.discount_value, subtotal)
                # Clamp so a flat-amount discount larger than the subtotal (or a stacked
                # percent discount on a tiny order) can never push the total negative.
                discount_amount = min(discount_amount, subtotal)
                order.promo_code = promo_obj
                order.discount_amount = discount_amount
                PromoCode.objects.filter(id=promo_obj.id).update(times_used=F('times_used') + 1)

            order.total_price = subtotal - discount_amount
            order.save()
            from accounting.utils import post_sale_journal
            post_sale_journal(order, request.user)

        _log(request, 'place_order', order.order_number,
             detail=f"total={float(order.total_price):.2f} EGP, {len(cart)} item(s)")

        response = {
            "success": True,
            "order_number": order.order_number,
            "discount": float(discount_amount),
            "total": float(order.total_price),
        }
        return JsonResponse(response)

    except ValueError as e:
        return JsonResponse({"success": False, "error": str(e)}, status=400)
    except Exception:
        logger.exception("Unexpected error in create_order")
        return JsonResponse(
            {"success": False, "error": "Order could not be completed. Please try again."},
            status=500,
        )


@cashier_or_manager_required
def validate_promo(request):
    if request.method != "POST":
        return JsonResponse({"valid": False, "error": "Bad request"}, status=405)

    if _is_rate_limited(request, 'promo', max_attempts=20, window=60):
        return JsonResponse({"valid": False, "error": "Too many requests."}, status=429)

    content_type = request.content_type or ''
    if 'application/json' not in content_type:
        return JsonResponse({"valid": False, "error": "Invalid content type"}, status=400)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"valid": False, "error": "Invalid JSON"}, status=400)

    code = str(data.get("code", "")).strip().upper()[:30]
    subtotal = Decimal(str(data.get("subtotal", "0")))

    if not code:
        return JsonResponse({"valid": False, "error": "No code provided"})

    try:
        from django.utils import timezone as _tz
        promo = PromoCode.objects.get(code__iexact=code, is_active=True)
        today = _tz.now().date()
        if promo.valid_from and today < promo.valid_from:
            return JsonResponse({"valid": False, "error": f"Code not valid yet (starts {promo.valid_from})."})
        if promo.valid_until and today > promo.valid_until:
            return JsonResponse({"valid": False, "error": f"Code expired on {promo.valid_until}."})
        if promo.max_uses and promo.times_used >= promo.max_uses:
            return JsonResponse({"valid": False, "error": "Code has reached its usage limit."})
        if promo.discount_type == 'percent':
            discount = (subtotal * promo.discount_value / Decimal("100")).quantize(Decimal("0.01"))
            label = f"{promo.discount_value}% off"
        else:
            discount = min(promo.discount_value, subtotal)
            label = f"{promo.discount_value} EGP off"
        _record_attempt(request, 'promo', window=60)
        return JsonResponse({
            "valid": True, "discount": float(discount),
            "label": label, "description": promo.description,
            "type": promo.discount_type, "raw_value": float(promo.discount_value),
        })
    except PromoCode.DoesNotExist:
        _record_attempt(request, 'promo', window=60)
        return JsonResponse({"valid": False, "error": "Invalid or inactive promo code"})


@cashier_or_manager_required
def receipt_view(request, order_number):
    order = get_object_or_404(Order, order_number=order_number)
    return render(request, "core/receipt.html", {"order": order})


# ═══════════════════════════════════════════════════════════════════════════════
#  DASHBOARD
# ═══════════════════════════════════════════════════════════════════════════════

@manager_required
def dashboard_view(request):
    _log(request, 'view_dashboard')
    import json as _json

    ingredients = Ingredient.objects.all()
    orders = Order.objects.filter(status='active').prefetch_related(
        'items__menu_item').order_by('-created_at')[:50]
    total_sales = Order.objects.filter(status='active').aggregate(
        Sum('total_price'))['total_price__sum'] or 0

    low_stock = [i for i in ingredients if i.stock_quantity <= i.low_stock_threshold]

    from django.db.models.functions import ExtractHour
    thirty_days_ago = timezone.now() - timezone.timedelta(days=30)
    hourly = (Order.objects
              .filter(status='active', created_at__gte=thirty_days_ago)
              .annotate(hour=ExtractHour('created_at'))
              .values('hour')
              .annotate(cnt=Count('id'))
              .order_by('hour'))
    hour_data = {h: 0 for h in range(24)}
    for row in hourly:
        hour_data[row['hour']] = row['cnt']
    chart_hours = list(range(24))
    chart_counts = [hour_data[h] for h in chart_hours]

    monthly_payroll = sum(
        (e.salary for e in Employee.objects.filter(is_active=True)), Decimal('0'))
    monthly_expenses = sum(
        (e.amount for e in Expense.objects.filter(
            is_voided=False, date__month=timezone.now().month, date__year=timezone.now().year
        )), Decimal('0'))
    fixed_costs = monthly_payroll + monthly_expenses

    recent_orders = Order.objects.filter(status='active', created_at__gte=thirty_days_ago)
    recent_revenue = sum(o.total_price for o in recent_orders)
    recent_cogs = Decimal('0')
    recent_count = recent_orders.count()
    for o in recent_orders.prefetch_related('items'):
        for oi in o.items.all():
            recent_cogs += line_cogs(oi)

    avg_order_value = (recent_revenue / recent_count).quantize(Decimal('0.01')) if recent_count else Decimal('0')
    avg_gross_margin = float((recent_revenue - recent_cogs) / recent_revenue * 100) if recent_revenue else 0

    daily_fixed = float(fixed_costs / 30) if fixed_costs else 0
    contrib_per_order = float(avg_order_value) * (avg_gross_margin / 100) if avg_order_value else 0
    breakeven_orders = int(daily_fixed / contrib_per_order) + 1 if contrib_per_order > 0 else 0
    today_orders = Order.objects.filter(
        status='active', created_at__date=timezone.now().date()).count()

    return render(request, 'core/dashboard.html', {
        'ingredients': ingredients,
        'orders': orders,
        'total_sales': total_sales,
        'low_stock': low_stock,
        'chart_hours': _json.dumps(chart_hours),
        'chart_counts': _json.dumps(chart_counts),
        'fixed_costs': fixed_costs,
        'daily_fixed': round(daily_fixed, 2),
        'breakeven_orders': breakeven_orders,
        'today_orders': today_orders,
        'avg_order_value': avg_order_value,
        'avg_gross_margin': round(avg_gross_margin, 1),
        'monthly_payroll': monthly_payroll,
        'monthly_expenses': monthly_expenses,
    })


@manager_required
def manage_orders(request):
    from decimal import Decimal
    _log(request, 'view_dashboard', detail='manage_orders')

    status_filter = request.GET.get('status', 'active')
    date_from_str = request.GET.get('date_from', '')
    date_to_str = request.GET.get('date_to', '')
    search = request.GET.get('q', '').strip()[:50]

    orders = Order.objects.prefetch_related('items__menu_item').order_by('-created_at')

    if status_filter in ('active', 'voided'):
        orders = orders.filter(status=status_filter)

    if date_from_str:
        try:
            orders = orders.filter(created_at__date__gte=datetime.strptime(date_from_str, '%Y-%m-%d').date())
        except ValueError:
            pass
    if date_to_str:
        try:
            orders = orders.filter(created_at__date__lte=datetime.strptime(date_to_str, '%Y-%m-%d').date())
        except ValueError:
            pass
    if search:
        orders = orders.filter(order_number__icontains=search)

    paginator = Paginator(orders, 25)
    page_obj = paginator.get_page(request.GET.get('page'))

    total_all = orders.aggregate(Sum('total_price'))['total_price__sum'] or Decimal('0')
    voided_count = Order.objects.filter(status='voided').count()
    active_count = Order.objects.filter(status='active').count()

    return render(request, 'core/manage_orders.html', {
        'orders': page_obj,
        'page_obj': page_obj,
        'status_filter': status_filter,
        'date_from': date_from_str,
        'date_to': date_to_str,
        'search': search,
        'total_all': total_all,
        'active_count': active_count,
        'voided_count': voided_count,
    })


# ═══════════════════════════════════════════════════════════════════════════════
#  SALES REPORT
# ═══════════════════════════════════════════════════════════════════════════════

@manager_required
def sales_report(request):
    _log(request, 'view_sales_report',
         detail=f"from={request.GET.get('date_from', '')} to={request.GET.get('date_to', '')}")
    import json as _json

    date_from_str = request.GET.get('date_from', '')
    date_to_str = request.GET.get('date_to', '')

    orders = Order.objects.filter(status='active').prefetch_related(
        'items__menu_item'
    ).order_by('-created_at')

    if date_from_str:
        try:
            orders = orders.filter(
                created_at__date__gte=datetime.strptime(date_from_str, '%Y-%m-%d').date())
        except ValueError:
            pass
    if date_to_str:
        try:
            orders = orders.filter(
                created_at__date__lte=datetime.strptime(date_to_str, '%Y-%m-%d').date())
        except ValueError:
            pass

    order_revenue = sum((o.total_price for o in orders), Decimal('0'))
    total_discounts = sum((o.discount_amount for o in orders), Decimal('0'))
    total_orders = orders.count()
    total_items_sold = sum(oi.quantity for o in orders for oi in o.items.all())

    from accounting.reporting import profit_loss_totals
    pnl = profit_loss_totals(date_from_str, date_to_str)
    total_revenue = pnl['sales']
    total_cogs = pnl['cogs']
    item_stats = {}
    daily_data = {}
    unknown_cost_count = 0

    for order in orders:
        day = order.created_at.strftime('%Y-%m-%d')
        if day not in daily_data:
            daily_data[day] = {'revenue': Decimal('0'), 'cost': Decimal('0')}
        daily_data[day]['revenue'] += order.total_price

        order_items = list(order.items.all())
        net_revenues = net_line_revenues(order, order_items)
        for oi in order_items:
            if not oi.stock_snapshot_ready and not oi.cogs_at_sale:
                unknown_cost_count += 1
            item_cost = line_cogs(oi)
            daily_data[day]['cost'] += item_cost

            name = oi.menu_item.name if oi.menu_item else '[Deleted menu item]'
            if name not in item_stats:
                item_stats[name] = {'name': name, 'qty': 0, 'revenue': Decimal('0'), 'cost': Decimal('0')}
            item_stats[name]['qty'] += oi.quantity
            item_stats[name]['revenue'] += net_revenues[oi.pk]
            item_stats[name]['cost'] += item_cost

    gross_profit = pnl['gross_profit']
    all_employees = Employee.objects.filter(is_active=True)
    monthly_payroll = sum((employee.salary for employee in all_employees), Decimal('0'))
    period_payroll = pnl['payroll']

    expenses_qs = Expense.objects.filter(is_voided=False)
    if date_from_str:
        try:
            expenses_qs = expenses_qs.filter(
                date__gte=datetime.strptime(date_from_str, '%Y-%m-%d').date())
        except ValueError:
            pass
    if date_to_str:
        try:
            expenses_qs = expenses_qs.filter(
                date__lte=datetime.strptime(date_to_str, '%Y-%m-%d').date())
        except ValueError:
            pass
    total_expenses = pnl['operating']
    expense_by_cat = {}
    for exp in expenses_qs:
        cat = exp.get_category_display()
        expense_by_cat[cat] = expense_by_cat.get(cat, Decimal('0')) + exp.amount

    net_profit = pnl['net_profit']
    gross_margin = float(gross_profit / total_revenue * 100) if total_revenue else 0
    net_margin = float(net_profit / total_revenue * 100) if total_revenue else 0

    items_list = sorted(item_stats.values(), key=lambda x: x['qty'], reverse=True)
    for it in items_list:
        it['profit'] = it['revenue'] - it['cost']
        it['margin'] = float(it['profit'] / it['revenue'] * 100) if it['revenue'] else 0

    top_items = items_list[:5]
    bottom_items = sorted(items_list, key=lambda x: x['margin'])[:3]
    employees = list(all_employees.order_by('job_title', 'full_name'))

    sorted_days = sorted(daily_data.keys())[-30:]
    chart_labels = sorted_days
    chart_revenue = [float(daily_data[d]['revenue']) for d in sorted_days]
    chart_cost = [float(daily_data[d]['cost']) for d in sorted_days]
    chart_profit = [round(r - c, 2) for r, c in zip(chart_revenue, chart_cost)]

    return render(request, 'core/sales_report.html', {
        'date_from': date_from_str, 'date_to': date_to_str,
        'total_revenue': total_revenue, 'total_discounts': total_discounts,
        'total_orders': total_orders, 'total_items_sold': total_items_sold,
        'total_cogs': total_cogs, 'gross_profit': gross_profit,
        'gross_margin': round(gross_margin, 1), 'period_payroll': period_payroll,
        'total_expenses': total_expenses, 'expense_by_cat': expense_by_cat,
        'damage_expense': pnl['damage'], 'other_expenses': pnl['other_expenses'],
        'other_revenue': pnl['other_revenue'], 'order_revenue': order_revenue,
        'unknown_cost_count': unknown_cost_count,
        'net_profit': net_profit, 'net_margin': round(net_margin, 1),
        'top_items': top_items, 'bottom_items': bottom_items,
        'all_items': items_list,
        'employees': employees, 'monthly_payroll': monthly_payroll,
        'chart_labels': _json.dumps(chart_labels),
        'chart_revenue': _json.dumps(chart_revenue),
        'chart_cost': _json.dumps(chart_cost),
        'chart_profit': _json.dumps(chart_profit),
        'orders': orders,
    })


# ═══════════════════════════════════════════════════════════════════════════════
#  USER MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

@manager_required
def manage_users(request):
    caller_role = request.user.profile.role
    is_super = (caller_role == 'superadmin')

    if request.method == 'POST':
        if not is_super:
            messages.error(request, "Only the Super Admin can create new accounts.")
            return redirect('manage_users')

        username = request.POST.get('username', '').strip()[:150]
        password = request.POST.get('password', '').strip()
        password2 = request.POST.get('password2', '').strip()
        role = request.POST.get('role', 'cashier')

        errors = []
        if not username:
            errors.append("Username is required.")
        if not password:
            errors.append("Password is required.")
        elif len(password) < 8:
            errors.append("Password must be at least 10 characters.")
        elif password != password2:
            errors.append("Passwords do not match.")
        if username and User.objects.filter(username=username).exists():
            errors.append(f"Username '{username}' is already taken.")
        if role not in ('cashier', 'manager', 'superadmin'):
            errors.append("Invalid role selected.")

        if errors:
            for e in errors:
                messages.error(request, e)
        else:
            user = User.objects.create_user(username=username, password=password)
            profile, _ = UserProfile.objects.get_or_create(user=user)
            profile.role = role
            profile.save()
            messages.success(request, f"Account '{username}' created as {role}.")
            _log(request, 'add_user', username, detail=f"role={role}")
            return redirect('manage_users')

    users = User.objects.all().select_related('profile').order_by('profile__role', 'username')
    return render(request, 'core/user_management.html', {'users': users, 'is_super': is_super})


@manager_required
def edit_user(request, user_id):
    target = get_object_or_404(User, id=user_id)
    caller = request.user
    caller_role = caller.profile.role
    is_super = (caller_role == 'superadmin')
    target_role = target.profile.role if hasattr(target, 'profile') else 'cashier'
    is_own = (caller.id == target.id)

    if not is_super:
        if not is_own and target_role != 'cashier':
            messages.error(request, "You don't have permission to edit this account.")
            return redirect('manage_users')

    if request.method == 'POST':
        new_username = request.POST.get('username', '').strip()[:150]
        new_password = request.POST.get('new_password', '').strip()
        new_password2 = request.POST.get('new_password2', '').strip()
        old_password = request.POST.get('old_password', '').strip()
        new_role = request.POST.get('role', target_role)

        errors = []

        if new_username and new_username != target.username:
            if User.objects.filter(username=new_username).exclude(id=user_id).exists():
                errors.append(f"Username '{new_username}' is already taken.")
            else:
                target.username = new_username

        if is_super:
            if new_role in ('superadmin', 'manager', 'cashier'):
                profile, _ = UserProfile.objects.get_or_create(user=target)
                profile.role = new_role
                profile.save()

        if new_password or old_password:
            if not old_password:
                errors.append("Current password is required to set a new one.")
            elif not target.check_password(old_password):
                errors.append("Current password is incorrect.")
                _record_attempt(request, 'login')
            elif len(new_password) < 8:
                errors.append("New password must be at least 10 characters.")
            elif new_password != new_password2:
                errors.append("New passwords do not match.")
            else:
                target.set_password(new_password)
                _log(request, 'edit_user', target.username,
                     detail=f"password changed by {caller.username}")

        if errors:
            for e in errors:
                messages.error(request, e)
            return redirect('manage_users')

        target.save()
        messages.success(request, f"Account '{target.username}' updated.")
        _log(request, 'edit_user', target.username, detail=f"edited by {caller.username}")
        return redirect('manage_users')

    return redirect('manage_users')


@manager_required
def delete_user(request, user_id):
    if request.method != 'POST':
        return redirect('manage_users')
    if request.user.id == user_id:
        messages.error(request, "You cannot delete your own account.")
        return redirect('manage_users')
    target = get_object_or_404(User, id=user_id)
    if request.user.profile.role != 'superadmin':
        messages.error(request, "Only the Super Admin can delete accounts.")
        return redirect('manage_users')
    username = target.username
    try:
        target.delete()
        _log(request, 'delete_user', username)
        messages.success(request, f"User '{username}' deleted.")
    except Exception:
        messages.error(request, f"Could not delete '{username}'.")
    return redirect('manage_users')


# ═══════════════════════════════════════════════════════════════════════════════
#  INGREDIENT MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

@manager_required
def manage_ingredients(request):
    if request.method == 'POST':
        action = request.POST.get('action', 'add')

        if action == 'add':
            name = request.POST.get('name', '').strip()[:100]
            unit = request.POST.get('unit', 'kg')
            if unit not in ('kg', 'L', 'pieces', 'boxes'):
                unit = 'kg'
            if not name:
                messages.error(request, 'Ingredient name is required.')
            elif Ingredient.objects.filter(name__iexact=name).exists():
                messages.error(request, f"Ingredient '{name}' already exists.")
            else:
                if unit == 'boxes':
                    num_boxes = Decimal(request.POST.get('num_boxes', '0') or '0')
                    units_per_box = max(1, int(request.POST.get('units_per_box', '1') or '1'))
                    sub_unit = request.POST.get('sub_unit', 'packets')
                    if sub_unit not in ('cans', 'packets'):
                        sub_unit = 'packets'
                    total_cost = Decimal(request.POST.get('total_cost', '0') or '0')
                    total_units = num_boxes * units_per_box
                    cpu = (total_cost / total_units).quantize(Decimal('0.0001')) if total_units else Decimal('0')
                    Ingredient.objects.create(
                        name=name, unit='boxes',
                        stock_quantity=total_units,
                        cost_per_unit=cpu,
                        units_per_box=units_per_box,
                        sub_unit=sub_unit,
                    )
                    _log(request, 'add_ingredient', name,
                         detail=f"unit=boxes sub={sub_unit} qty={total_units:.0f} cost/unit={cpu}")
                    messages.success(request, f"'{name}' added.")
                else:
                    raw_qty = Decimal(request.POST.get('stock_quantity', '0') or '0')
                    cost_per_unit = Decimal(request.POST.get('cost_per_unit', '0') or '0')
                    Ingredient.objects.create(
                        name=name, unit=unit,
                        stock_quantity=raw_qty,
                        cost_per_unit=cost_per_unit,
                    )
                    _log(request, 'add_ingredient', name,
                         detail=f"unit={unit} qty={raw_qty} cost/unit={cost_per_unit}")
                    messages.success(request, f"Ingredient '{name}' added.")

        elif action == 'restock':
            messages.error(request, 'Receive new stock through a Purchase Order so inventory and accounting stay in sync.')

        elif action == 'edit':
            ing_id = request.POST.get('ingredient_id')
            try:
                ingredient = get_object_or_404(Ingredient, id=ing_id)
                new_name = request.POST.get('name', ingredient.name).strip()[:100]
                if new_name:
                    ingredient.name = new_name
                new_unit = request.POST.get('unit', ingredient.unit)
                if new_unit in ('kg', 'L', 'pieces', 'boxes'):
                    ingredient.unit = new_unit

                if ingredient.unit == 'boxes':
                    new_upb = request.POST.get('edit_units_per_box', '').strip()
                    new_sub = request.POST.get('edit_sub_unit', '').strip()
                    box_cpu = request.POST.get('cost_per_unit', '').strip()
                    if new_upb:
                        ingredient.units_per_box = max(1, int(new_upb))
                    if new_sub and new_sub in ('cans', 'packets'):
                        ingredient.sub_unit = new_sub
                    if box_cpu:
                        ingredient.cost_per_unit = Decimal(box_cpu.replace(',', '.').split()[0])
                else:
                    raw_cpu = request.POST.get('cost_per_unit', '').strip()
                    if raw_cpu:
                        ingredient.cost_per_unit = Decimal(raw_cpu.replace(',', '.').split()[0])

                raw_thr = request.POST.get('low_stock_threshold', '').strip()
                if raw_thr:
                    ingredient.low_stock_threshold = Decimal(raw_thr.replace(',', '.').split()[0])

                ingredient.save()
                _log(request, 'edit_ingredient', ingredient.name,
                     detail=f"threshold={ingredient.low_stock_threshold} cost={ingredient.cost_per_unit}")
                messages.success(request, f"Ingredient '{ingredient.name}' updated.")
            except Exception:
                logger.exception("Error editing ingredient")
                messages.error(request, "Error updating ingredient.")

        return redirect('manage_ingredients_page')
    return redirect('manage_ingredients_page')


@manager_required
def manual_stock_adjust(request):
    if request.method != 'POST':
        return redirect('dashboard')
    ing_id = request.POST.get('ingredient_id')
    action_type = request.POST.get('action_type', 'add')
    note = str(request.POST.get('note', ''))[:200].strip()
    if not note:
        messages.error(request, 'Give a reason for the stock correction.')
        return redirect('dashboard')

    if action_type not in ('add', 'subtract', 'set'):
        messages.error(request, 'Invalid action type.')
        return redirect('dashboard')

    try:
        ingredient = get_object_or_404(Ingredient, id=ing_id)
        raw_qty = request.POST.get('quantity', '').strip()
        if not raw_qty:
            messages.error(request, 'Please enter a quantity.')
            return redirect('dashboard')
        qty = Decimal(raw_qty)
        if not qty.is_finite() or qty < 0 or qty.as_tuple().exponent < -3:
            messages.error(request, 'Quantity must be positive.')
            return redirect('dashboard')
        with transaction.atomic():
            ingredient = Ingredient.objects.select_for_update().get(pk=ingredient.pk)
            old_qty = ingredient.stock_quantity
            if ingredient.cost_per_unit < 0:
                raise ValueError('Ingredient cost is invalid. Correct it before adjusting stock.')
            if action_type == 'add':
                change = qty
                ingredient.stock_quantity = old_qty + qty
                verb = f'+{qty}'
            elif action_type == 'subtract':
                if qty > old_qty:
                    raise ValueError('Cannot subtract more than available stock.')
                change = -qty
                ingredient.stock_quantity = old_qty - qty
                verb = f'-{qty}'
            else:
                change = qty - old_qty
                ingredient.stock_quantity = qty
                verb = f'set to {qty}'

            ingredient.save(update_fields=['stock_quantity'])
            stock_log = _log_stock(ingredient, change, 'manual', request.user,
                                   note=note)
            from accounting.utils import post_stock_adjustment_journal
            post_stock_adjustment_journal(ingredient, change, request.user, stock_log)
        _log(request, 'edit_ingredient', ingredient.name,
             detail=f'Manual stock adjustment: {verb} {ingredient.unit}. Note: {note}')
        messages.success(request,
                         f"Stock for '{ingredient.name}' updated: {verb} {ingredient.unit}. "
                         f"New stock: {ingredient.stock_quantity} {ingredient.unit}.")
    except (ValueError, InvalidOperation) as exc:
        messages.error(request, str(exc))
    except Exception:
        logger.exception('Error adjusting stock')
        messages.error(request, 'Error adjusting stock.')

    return redirect('dashboard')


def _build_menu_cost(menu_items_raw):
    result = []
    for item in menu_items_raw:
        total_cost = 0.0
        breakdown = []
        for recipe_item in item.ingredients.all():
            ing = recipe_item.ingredient
            sq = float(_convert_to_stock_unit(recipe_item.quantity_required, recipe_item.recipe_unit, ing.unit))
            line_cost = sq * float(ing.cost_per_unit)
            total_cost += line_cost
            breakdown.append({'name': ing.name, 'qty': recipe_item.quantity_required,
                              'unit': ing.unit, 'cost_per_unit': ing.cost_per_unit,
                              'line_cost': line_cost})
        profit = float(item.price) - total_cost
        margin = (profit / float(item.price) * 100) if float(item.price) > 0 else 0
        result.append({'name': item.name, 'price': item.price, 'is_active': item.is_active,
                       'total_cost': total_cost, 'profit': profit, 'margin': margin,
                       'ingredient_breakdown': breakdown})
    return result


# ═══════════════════════════════════════════════════════════════════════════════
#  MENU MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

@manager_required
def manage_menu(request):
    menu_items = MenuItem.objects.prefetch_related('ingredients__ingredient').all()
    ingredients = Ingredient.objects.all()
    categories = MenuCategory.objects.all()
    if request.method == 'POST':
        name = str(request.POST.get('name', ''))[:100]
        price = request.POST.get('price')
        description = str(request.POST.get('description', ''))[:2000]
        image_url = str(request.POST.get('image_url', ''))[:500]
        is_active = request.POST.get('is_active') == 'on'
        category_id = request.POST.get('category', '')
        ingredient_ids = request.POST.getlist('ingredients')
        quantities = request.POST.getlist('quantities')
        recipe_units = request.POST.getlist('recipe_units')
        if name and price:
            try:
                with transaction.atomic():
                    cat = MenuCategory.objects.get(id=category_id) if category_id else None
                    menu_item = MenuItem.objects.create(
                        name=name, price=price, description=description,
                        image_url=image_url, is_active=is_active, category=cat)
                    for i_id, qty, runit in zip(ingredient_ids, quantities, recipe_units):
                        if i_id and qty and float(qty) > 0:
                            ingredient = get_object_or_404(Ingredient, id=i_id)
                            if not _validate_recipe_unit(ingredient, runit):
                                raise ValueError(
                                    f"Unit '{runit}' is not valid for '{ingredient.name}'")
                            MenuItemIngredient.objects.create(
                                menu_item=menu_item, ingredient=ingredient,
                                quantity_required=qty, recipe_unit=runit)
                    messages.success(request, f"Menu item '{name}' created.")
                    _log(request, 'add_menu_item', name, detail=f"price={price} category={cat}")
                    return redirect('manage_menu')
            except Exception as e:
                messages.error(request, f"Error creating menu item: {e}")
        else:
            messages.error(request, "Name and price are required.")
    return render(request, 'core/menu_management.html', {
        'menu_items': menu_items, 'ingredients': ingredients, 'categories': categories,
        'menu_items_with_cost': _build_menu_cost(menu_items),
    })


@manager_required
def edit_menu_item(request, pk):
    menu_item = get_object_or_404(MenuItem, pk=pk)
    ingredients = Ingredient.objects.all()
    categories = MenuCategory.objects.all()
    if request.method == 'POST':
        menu_item.name = str(request.POST.get('name', ''))[:100]
        menu_item.price = request.POST.get('price')
        menu_item.description = str(request.POST.get('description', ''))[:2000]
        menu_item.image_url = str(request.POST.get('image_url', ''))[:500]
        menu_item.is_active = request.POST.get('is_active') == 'on'
        category_id = request.POST.get('category', '')
        menu_item.category = MenuCategory.objects.get(id=category_id) if category_id else None
        ingredient_ids = request.POST.getlist('ingredients')
        quantities = request.POST.getlist('quantities')
        recipe_units = request.POST.getlist('recipe_units')
        if menu_item.name and menu_item.price:
            try:
                with transaction.atomic():
                    menu_item.save()
                    menu_item.ingredients.all().delete()
                    for i_id, qty, runit in zip(ingredient_ids, quantities, recipe_units):
                        if i_id and qty and float(qty) > 0:
                            ingredient = get_object_or_404(Ingredient, id=i_id)
                            if not _validate_recipe_unit(ingredient, runit):
                                raise ValueError(
                                    f"Unit '{runit}' is not valid for '{ingredient.name}'")
                            MenuItemIngredient.objects.create(
                                menu_item=menu_item, ingredient=ingredient,
                                quantity_required=qty, recipe_unit=runit)
                    messages.success(request, f"Menu item '{menu_item.name}' updated.")
                    _log(request, 'edit_menu_item', menu_item.name,
                         detail=f"price={menu_item.price}")
                    return redirect('manage_menu')
            except Exception as e:
                messages.error(request, f"Error updating menu item: {e}")
        else:
            messages.error(request, "Name and price are required.")
    return render(request, 'core/edit_menu_item.html', {
        'menu_item': menu_item, 'ingredients': ingredients, 'categories': categories,
        'current_ingredients': menu_item.ingredients.all(),
    })


@manager_required
def toggle_menu_item_status(request, pk):
    menu_item = get_object_or_404(MenuItem, pk=pk)
    menu_item.is_active = not menu_item.is_active
    menu_item.save()
    status = "activated" if menu_item.is_active else "deactivated"
    _log(request, 'toggle_menu_item', menu_item.name, detail=status)
    messages.success(request, f"Menu item '{menu_item.name}' {status}.")
    return redirect('manage_menu')


@manager_required
def manage_ingredients_page(request):
    from django.db.models import F, ExpressionWrapper, DecimalField as _DFld
    ingredients = (Ingredient.objects
                   .annotate(total_value=ExpressionWrapper(
        F('stock_quantity') * F('cost_per_unit'),
        output_field=_DFld(max_digits=16, decimal_places=4)))
                   .order_by('name'))
    grand_total = sum(float(i.total_value or 0) for i in ingredients)
    return render(request, 'core/manage_ingredients.html', {
        'ingredients': ingredients,
        'grand_total': grand_total,
    })


# ═══════════════════════════════════════════════════════════════════════════════
#  PDF REPORT HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from django.http import HttpResponse

PAGE_W = 17 * cm
ORANGE = colors.HexColor('#FF4500')
LIGHT_ORANGE = colors.HexColor('#FFF5F2')
GREY = colors.HexColor('#6b7280')
LIGHT_GREY = colors.HexColor('#f3f4f6')
MID_GREY = colors.HexColor('#e5e7eb')


def _make_doc(buf):
    return SimpleDocTemplate(buf, pagesize=A4,
                             leftMargin=2 * cm, rightMargin=2 * cm, topMargin=2 * cm, bottomMargin=2 * cm)


def _title_styles():
    title = ParagraphStyle('RPTitle', fontSize=18, textColor=ORANGE,
                           fontName='Helvetica-Bold', spaceAfter=3, leading=22)
    sub = ParagraphStyle('RPSub', fontSize=9, textColor=GREY,
                         fontName='Helvetica', spaceAfter=0)
    bold8 = ParagraphStyle('RPBold8', fontSize=8, fontName='Helvetica-Bold')
    norm8 = ParagraphStyle('RPNorm8', fontSize=8, fontName='Helvetica')
    return title, sub, bold8, norm8


def _base_table_style():
    return TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), ORANGE),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 8),
        ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 1), (-1, -1), 8),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, LIGHT_GREY]),
        ('GRID', (0, 0), (-1, -1), 0.3, MID_GREY),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
    ])


def _get_filtered_orders(request):
    date_from_str = request.GET.get('date_from', '')
    date_to_str = request.GET.get('date_to', '')
    orders = Order.objects.filter(status='active').prefetch_related(
        'items__menu_item__ingredients__ingredient').order_by('created_at')
    if date_from_str:
        try:
            orders = orders.filter(
                created_at__date__gte=datetime.strptime(date_from_str, '%Y-%m-%d').date())
        except ValueError:
            pass
    if date_to_str:
        try:
            orders = orders.filter(
                created_at__date__lte=datetime.strptime(date_to_str, '%Y-%m-%d').date())
        except ValueError:
            pass
    return orders, date_from_str, date_to_str


def _period_label(date_from_str, date_to_str):
    if date_from_str and date_to_str:
        return f"{date_from_str}  to  {date_to_str}"
    elif date_from_str:
        return f"From {date_from_str}"
    elif date_to_str:
        return f"Until {date_to_str}"
    return "All Time"


@manager_required
def pdf_report_financial(request):
    orders, date_from_str, date_to_str = _get_filtered_orders(request)
    _log(request, 'export_pdf', 'Financial Report',
         detail=f"from={date_from_str} to={date_to_str}")
    title_style, sub_style, bold8, norm8 = _title_styles()
    buf = BytesIO()
    doc = _make_doc(buf)
    story = []
    story.append(Paragraph("Searing Sandwiches", title_style))
    story.append(Paragraph("Financial Report",
                           ParagraphStyle('sub2', fontSize=13, textColor=GREY, fontName='Helvetica', spaceAfter=3)))
    story.append(Paragraph(f"Period: {_period_label(date_from_str, date_to_str)}", sub_style))
    story.append(Spacer(1, 0.3 * cm))
    story.append(HRFlowable(width="100%", thickness=1.5, color=ORANGE))
    story.append(Spacer(1, 0.4 * cm))
    headers = [Paragraph(f'<b>{h}</b>', bold8) for h in
               ['Order #', 'Date', 'Time', 'Product', 'Unit Cost', 'Sale Price', 'Qty', 'Full Cost', 'Revenue',
                'Profit']]
    col_w = [2.0 * cm, 2.2 * cm, 1.6 * cm, 3.2 * cm, 2.0 * cm, 2.0 * cm, 1.0 * cm, 2.0 * cm, 2.0 * cm, 2.0 * cm]
    rows = [headers]
    grand_cost = grand_revenue = grand_profit = 0.0
    for order in orders:
        order_date = order.created_at.strftime('%Y-%m-%d')
        order_time = order.created_at.strftime('%H:%M')
        order_items = list(order.items.all())
        net_revenues = net_line_revenues(order, order_items)
        for oi in order_items:
            qty = oi.quantity;
            full_cost = float(line_cogs(oi))
            unit_cost = full_cost / qty if qty else 0
            sale_price = float(oi.price_at_order)
            full_revenue = float(net_revenues[oi.pk])
            profit = full_revenue - full_cost
            grand_cost += full_cost;
            grand_revenue += full_revenue;
            grand_profit += profit
            rows.append([
                Paragraph(order.order_number, norm8),
                Paragraph(order_date, norm8),
                Paragraph(order_time, norm8),
                Paragraph(oi.menu_item.name if oi.menu_item else '[Deleted menu item]', norm8),
                Paragraph(f"{unit_cost:.2f}", norm8),
                Paragraph(f"{sale_price:.2f}", norm8),
                Paragraph(str(qty), norm8),
                Paragraph(f"{full_cost:.2f}", norm8),
                Paragraph(f"{full_revenue:.2f}", norm8),
                Paragraph(f"{profit:.2f}", norm8),
            ])
    rows.append([
        Paragraph('<b>TOTAL</b>', bold8), '', '', '', '', '', '',
        Paragraph(f'<b>{grand_cost:.2f}</b>', bold8),
        Paragraph(f'<b>{grand_revenue:.2f}</b>', bold8),
        Paragraph(f'<b>{grand_profit:.2f}</b>', bold8),
    ])
    ts = _base_table_style()
    ts.add('BACKGROUND', (0, -1), (-1, -1), LIGHT_ORANGE)
    ts.add('LINEABOVE', (0, -1), (-1, -1), 1.2, ORANGE)
    ts.add('SPAN', (0, -1), (6, -1))
    t = Table(rows, colWidths=col_w, repeatRows=1)
    t.setStyle(ts)
    story.append(t)
    story.append(Spacer(1, 0.6 * cm))
    from accounting.reporting import profit_loss_totals
    pnl = profit_loss_totals(date_from_str, date_to_str)
    net_profit_val = float(pnl['net_profit'])
    summary = [
        [Paragraph('Posted Sales Revenue', norm8), Paragraph(f'{pnl["sales"]:.2f} EGP', norm8)],
        [Paragraph('Posted COGS', norm8), Paragraph(f'{pnl["cogs"]:.2f} EGP', norm8)],
        [Paragraph('Posted Gross Profit', norm8), Paragraph(f'{pnl["gross_profit"]:.2f} EGP', norm8)],
        [Paragraph('Other Revenue', norm8), Paragraph(f'{pnl["other_revenue"]:.2f} EGP', norm8)],
        [Paragraph('Payroll Posted', norm8), Paragraph(f'- {pnl["payroll"]:.2f} EGP',
                                                        ParagraphStyle('red', fontName='Helvetica', fontSize=8,
                                                                       textColor=colors.HexColor('#dc2626')))],
        [Paragraph('Other Expenses incl. Damage', norm8), Paragraph(f'- {pnl["operating"] + pnl["damage"] + pnl["other_expenses"]:.2f} EGP', norm8)],
        [Paragraph('<b>Net Profit</b>', bold8), Paragraph(f'<b>{net_profit_val:.2f} EGP</b>',
                                                          ParagraphStyle('profit', fontName='Helvetica-Bold',
                                                                         fontSize=8,
                                                                         textColor=ORANGE if net_profit_val >= 0 else colors.HexColor(
                                                                             '#dc2626')))],
    ]
    st = Table(summary, colWidths=[4.5 * cm, 4 * cm])
    st.setStyle(TableStyle([
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LINEBELOW', (0, 2), (-1, 2), 0.5, MID_GREY),
        ('LINEABOVE', (0, -1), (-1, -1), 1, ORANGE),
        ('BACKGROUND', (0, -1), (-1, -1), LIGHT_ORANGE),
    ]))
    story.append(st)
    doc.build(story)
    buf.seek(0)
    resp = HttpResponse(buf, content_type='application/pdf')
    resp['Content-Disposition'] = 'attachment; filename="financial_report.pdf"'
    return resp


@manager_required
def pdf_report_ingredients(request):
    orders, date_from_str, date_to_str = _get_filtered_orders(request)
    _log(request, 'export_pdf', 'Ingredient Usage Report',
         detail=f"from={date_from_str} to={date_to_str}")
    title_style, sub_style, bold8, norm8 = _title_styles()
    item_qty = {};
    item_names = {}
    for order in orders:
        for oi in order.items.all():
            if not oi.menu_item: continue
            mid = oi.menu_item_id
            item_qty[mid] = item_qty.get(mid, 0) + oi.quantity
            item_names[mid] = oi.menu_item
    buf = BytesIO();
    doc = _make_doc(buf);
    story = []
    story.append(Paragraph("Searing Sandwiches", title_style))
    story.append(Paragraph("Ingredient Usage Report",
                           ParagraphStyle('sub2', fontSize=13, textColor=GREY, fontName='Helvetica', spaceAfter=3)))
    story.append(Paragraph(f"Period: {_period_label(date_from_str, date_to_str)}", sub_style))
    story.append(Spacer(1, 0.3 * cm))
    story.append(HRFlowable(width="100%", thickness=1.5, color=ORANGE))
    story.append(Spacer(1, 0.4 * cm))
    headers = [Paragraph(f'<b>{h}</b>', bold8) for h in
               ['Product Name', 'Qty Sold', 'Ingredient', 'Qty Used', 'Unit', 'Ing. Cost (EGP)']]
    col_w = [4.5 * cm, 2.0 * cm, 4.5 * cm, 2.5 * cm, 1.5 * cm, 2.0 * cm]
    rows = [headers]
    for mid, menu_item in item_names.items():
        qty_sold = item_qty[mid]
        recipes = list(menu_item.ingredients.all())
        if not recipes:
            rows.append([Paragraph(menu_item.name, norm8), Paragraph(str(qty_sold), norm8),
                         Paragraph('—', norm8), Paragraph('—', norm8),
                         Paragraph('—', norm8), Paragraph('—', norm8)])
            continue
        for i, recipe in enumerate(recipes):
            ing = recipe.ingredient
            sq = float(_convert_to_stock_unit(recipe.quantity_required, recipe.recipe_unit, ing.unit))
            total_qty_used = sq * qty_sold
            ing_cost = total_qty_used * float(ing.cost_per_unit)
            rows.append([
                Paragraph(menu_item.name if i == 0 else '', norm8),
                Paragraph(str(qty_sold) if i == 0 else '', norm8),
                Paragraph(ing.name, norm8),
                Paragraph(f"{total_qty_used:.3f}", norm8),
                Paragraph(ing.unit, norm8),
                Paragraph(f"{ing_cost:.2f}", norm8),
            ])
        rows.append(['', '', '', '', '', ''])
    ts = _base_table_style()
    for i, row in enumerate(rows):
        if i > 0 and all(str(c) == '' for c in row):
            ts.add('LINEABOVE', (0, i), (-1, i), 0.6, MID_GREY)
            ts.add('TOPPADDING', (0, i), (-1, i), 2)
            ts.add('BOTTOMPADDING', (0, i), (-1, i), 2)
    t = Table(rows, colWidths=col_w, repeatRows=1)
    t.setStyle(ts)
    story.append(t)
    doc.build(story)
    buf.seek(0)
    resp = HttpResponse(buf, content_type='application/pdf')
    resp['Content-Disposition'] = 'attachment; filename="ingredient_usage_report.pdf"'
    return resp


# ═══════════════════════════════════════════════════════════════════════════════
#  INGREDIENT CRUD (delete / archive)
# ═══════════════════════════════════════════════════════════════════════════════

@manager_required
def delete_ingredient(request, ingredient_id):
    ingredient = get_object_or_404(Ingredient, id=ingredient_id)
    name = ingredient.name
    recipe_count = MenuItemIngredient.objects.filter(ingredient=ingredient).count()
    if recipe_count > 0:
        messages.error(request,
                       f"Cannot delete '{name}' — it is used in {recipe_count} recipe(s). "
                       f"Remove it from all recipes first.")
        return redirect('manage_ingredients_page')
    try:
        ingredient.delete()
        _log(request, 'delete_ingredient', name)
        messages.success(request, f"Ingredient '{name}' deleted.")
    except Exception:
        messages.error(request, f"Could not delete '{name}'.")
    return redirect('manage_ingredients_page')


@manager_required
def archive_ingredient(request, ingredient_id):
    ingredient = get_object_or_404(Ingredient, id=ingredient_id)
    ingredient.is_active = not ingredient.is_active
    ingredient.save()
    action = 'activated' if ingredient.is_active else 'archived'
    _log(request, 'edit_ingredient', ingredient.name,
         detail=f'{action} (is_active={ingredient.is_active})')
    messages.success(request, f"Ingredient '{ingredient.name}' {action}.")
    return redirect('manage_ingredients_page')


@manager_required
def delete_menu_item(request, pk):
    item = get_object_or_404(MenuItem, pk=pk)
    name = item.name
    try:
        item.delete()
        _log(request, 'delete_menu_item', name)
        messages.success(request, f"Menu item '{name}' deleted.")
    except Exception:
        messages.error(request, f"Cannot delete '{name}'.")
    return redirect('manage_menu')


@manager_required
def delete_order(request, order_id):
    if request.method != 'POST':
        return redirect('dashboard')
    get_object_or_404(Order, id=order_id)
    messages.error(request, 'Orders with stock and journal history cannot be deleted. Void the order with a reason instead.')
    return redirect('dashboard')


# ═══════════════════════════════════════════════════════════════════════════════
#  ORDER EDIT / VOID / DAMAGE
# ═══════════════════════════════════════════════════════════════════════════════

@manager_required
def edit_order(request, order_id):
    order = get_object_or_404(Order, id=order_id)
    if order.status == 'voided':
        messages.error(request, 'Cannot edit a voided order.')
        return redirect('dashboard')
    if request.method == 'POST':
        try:
            from .order_integrity import edit_existing_order
            order, changes = edit_existing_order(order_id, request.POST, request.user)
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect('edit_order', order_id=order_id)
        except Exception:
            logger.exception('Failed to edit order %s', order_id)
            messages.error(request, 'Order could not be updated. No changes were saved.')
            return redirect('edit_order', order_id=order_id)
        if changes:
            _log(request, 'edit_order', order.order_number, detail='; '.join(changes))
            messages.success(request, f'Order #{order.order_number} updated.')
        else:
            messages.info(request, 'No changes were made.')
        return redirect('dashboard')
    items = order.items.select_related('menu_item').all()
    return render(request, 'core/edit_order.html', {'order': order, 'items': items})

@manager_required
def log_damage(request, order_id):
    order = get_object_or_404(Order, id=order_id)

    if request.method == 'POST':
        with transaction.atomic():
            any_logged = False
            reason_global = str(request.POST.get('damage_reason_global', ''))[:200].strip() or 'Damaged during cooking'
            ing_damage = {}

            for oi_id in request.POST.getlist('damaged_oi_id'):
                portions_raw = request.POST.get(f'portions_{oi_id}', '1')
                ticked_raw = request.POST.get(f'ticked_{oi_id}', '')
                try:
                    portions = Decimal(portions_raw or '1')
                    if portions <= 0:
                        continue
                except Exception:
                    portions = Decimal('1')

                ticked_indices = set()
                for part in ticked_raw.split(','):
                    part = part.strip()
                    if part.isdigit():
                        ticked_indices.add(int(part))

                try:
                    from .models import OrderItem as OI
                    oi = OI.objects.select_related('menu_item').get(id=oi_id, order=order)
                except Exception:
                    continue

                if not oi.menu_item:
                    continue

                recipe_list = list(oi.menu_item.ingredients.select_related('ingredient').all())
                for idx, recipe in enumerate(recipe_list):
                    if idx not in ticked_indices:
                        continue
                    ing = recipe.ingredient
                    qty_in_stock_unit = _convert_to_stock_unit(
                        recipe.quantity_required * portions,
                        recipe.recipe_unit, ing.unit)
                    if ing.id not in ing_damage:
                        ing_damage[ing.id] = {'qty': Decimal('0'), 'unit': ing.unit}
                    ing_damage[ing.id]['qty'] += qty_in_stock_unit

            journal_records = []
            for ing_id, data in ing_damage.items():
                qty = data['qty']
                ing = Ingredient.objects.select_for_update().get(id=ing_id)
                unit_cost_snapshot = ing.cost_per_unit
                if not qty.is_finite() or qty <= 0 or qty > ing.stock_quantity:
                    transaction.set_rollback(True)
                    messages.error(request, f'Insufficient or invalid stock for {ing.name}. No damage was recorded.')
                    return redirect('dashboard')
                qty = qty.quantize(Decimal('0.001'))
                if qty == 0:
                    transaction.set_rollback(True)
                    messages.error(request, f'Damage quantity for {ing.name} is below stock precision.')
                    return redirect('dashboard')

                ing.stock_quantity -= qty
                ing.save()

                DamagedStock.objects.create(
                    order=order, ingredient=ing,
                    qty_damaged=qty, unit=data['unit'],
                    reason=reason_global,
                    recorded_by=request.user,
                )
                _log_stock(ing, -qty, 'damaged', request.user,
                           note=f'Damage — {reason_global} (Order {order.order_number})')
                journal_records.append({
                    'ingredient': ing,
                    'qty_damaged': qty,
                    'unit_cost': unit_cost_snapshot,
                })
                any_logged = True

            if any_logged:
                journal_posted = True
                try:
                    from accounting.utils import post_damage_journal
                    post_damage_journal(journal_records, order, request.user)
                except Exception:
                    journal_posted = False
                    logger.exception(
                        "Failed to post damage journal for order %s", order.order_number)
                    transaction.set_rollback(True)
                _log(request, 'log_damage', order.order_number,
                     detail=f"Damage logged from order {order.order_number}")
                total_loss = sum(r['qty_damaged'] * r['unit_cost'] for r in journal_records)
                if journal_posted:
                    messages.success(request,
                                     f"Damage logged for order #{order.order_number}. "
                                     f"Stock deducted and {float(total_loss):.2f} EGP posted to journal.")
                else:
                    messages.error(request, 'Damage could not be recorded. No changes were saved.')
            else:
                messages.warning(request, "No ingredients selected for damage.")

        return redirect('dashboard')

    order_items = []
    for oi in order.items.select_related('menu_item').prefetch_related(
            'menu_item__ingredients__ingredient').all():
        if not oi.menu_item:
            continue
        recipe_lines = []
        for recipe in oi.menu_item.ingredients.select_related('ingredient').all():
            ing = recipe.ingredient
            qty_per_portion = _convert_to_stock_unit(
                recipe.quantity_required, recipe.recipe_unit, ing.unit)
            recipe_lines.append({
                'name': ing.name,
                'qty_per_portion': qty_per_portion,
                'unit': ing.unit,
            })
        order_items.append({
            'oi_id': oi.id,
            'item_name': oi.menu_item.name,
            'ordered_qty': oi.quantity,
            'recipe': recipe_lines,
        })

    return render(request, 'core/log_damage.html', {
        'order': order, 'order_items': order_items,
    })


@manager_required
def damaged_stock_view(request):
    _log(request, 'view_stock_log', detail='damaged')
    from django.db.models import F, ExpressionWrapper, DecimalField as _DecF
    logs = (DamagedStock.objects
            .select_related('ingredient', 'order', 'recorded_by')
            .annotate(line_cost=ExpressionWrapper(
        F('qty_damaged') * F('ingredient__cost_per_unit'),
        output_field=_DecF(max_digits=14, decimal_places=4)
    ))
            .order_by('-created_at'))

    ing_filter = request.GET.get('ingredient', '')
    if ing_filter:
        logs = logs.filter(ingredient_id=ing_filter)

    paginator = Paginator(logs, 50)
    page_obj = paginator.get_page(request.GET.get('page'))
    from django.db.models import ExpressionWrapper, DecimalField
    total_damage_cost = (
            DamagedStock.objects
            .select_related('ingredient')
            .annotate(line_cost=ExpressionWrapper(
                F('qty_damaged') * F('ingredient__cost_per_unit'),
                output_field=DecimalField(max_digits=14, decimal_places=4)
            ))
            .aggregate(total=Sum('line_cost'))['total'] or 0
    )

    return render(request, 'core/damaged_stock.html', {
        'logs': page_obj,
        'page_obj': page_obj,
        'ingredients': Ingredient.objects.all().order_by('name'),
        'ing_filter': ing_filter,
        'total_damage_cost': total_damage_cost,
    })


# ═══════════════════════════════════════════════════════════════════════════════
#  VOID ORDER
# ═══════════════════════════════════════════════════════════════════════════════

@manager_required
def void_order(request, order_id):
    order = get_object_or_404(Order, id=order_id)
    if request.method != 'POST':
        return redirect('dashboard')
    reason = str(request.POST.get('void_reason', '')).strip()[:200]
    if not reason:
        messages.error(request, 'A reason is required to void an order.')
        return redirect('dashboard')
    try:
        from .order_integrity import void_existing_order
        order = void_existing_order(order_id, reason, request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect('dashboard')
    except Exception:
        logger.exception('Failed to void order %s', order_id)
        messages.error(request, 'Order could not be voided. No changes were saved.')
        return redirect('dashboard')
    _log(request, 'void_order', order.order_number, detail=f'reason={reason}')
    messages.success(request, f'Order #{order.order_number} voided. Stock and accounting were reversed.')
    return redirect('dashboard')

# ═══════════════════════════════════════════════════════════════════════════════
#  EMPLOYEE MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

@manager_required
def manage_employees(request):
    employees = Employee.objects.all().order_by('full_name')
    total_salary = sum(e.salary for e in employees if e.is_active)
    return render(request, 'core/manage_employees.html', {
        'employees': employees,
        'total_salary': total_salary,
        'job_choices': Employee.JOB_CHOICES,
    })


@manager_required
def add_employee(request):
    if request.method == 'POST':
        full_name = str(request.POST.get('full_name', '')).strip()[:150]
        phone = str(request.POST.get('phone', '')).strip()[:30]
        job_title = request.POST.get('job_title', 'cashier')
        salary = request.POST.get('salary', '0')
        hire_date = request.POST.get('hire_date', '')
        notes = str(request.POST.get('notes', '')).strip()[:1000]
        if job_title not in dict(Employee.JOB_CHOICES):
            job_title = 'cashier'
        if full_name:
            Employee.objects.create(
                full_name=full_name, phone=phone, job_title=job_title,
                salary=Decimal(salary or '0'),
                hire_date=hire_date or timezone.now().date(),
                notes=notes,
            )
            _log(request, 'add_employee', full_name, detail=f"role={job_title}")
            messages.success(request, f"Employee '{full_name}' added.")
        else:
            messages.error(request, "Full name is required.")
    return redirect('manage_employees')


@manager_required
def edit_employee(request, employee_id):
    emp = get_object_or_404(Employee, id=employee_id)
    if request.method == 'POST':
        emp.full_name = str(request.POST.get('full_name', emp.full_name)).strip()[:150]
        emp.phone = str(request.POST.get('phone', emp.phone)).strip()[:30]
        job = request.POST.get('job_title', emp.job_title)
        emp.job_title = job if job in dict(Employee.JOB_CHOICES) else emp.job_title
        emp.salary = Decimal(request.POST.get('salary', str(emp.salary)) or '0')
        hire_date = request.POST.get('hire_date', '')
        if hire_date: emp.hire_date = hire_date
        emp.notes = str(request.POST.get('notes', emp.notes)).strip()[:1000]
        emp.is_active = request.POST.get('is_active') == 'on'
        emp.save()
        _log(request, 'edit_employee', emp.full_name)
        messages.success(request, f"Employee '{emp.full_name}' updated.")
    return redirect('manage_employees')


@manager_required
def delete_employee(request, employee_id):
    emp = get_object_or_404(Employee, id=employee_id)
    name = emp.full_name
    emp.delete()
    _log(request, 'delete_employee', name)
    messages.success(request, f"Employee '{name}' removed.")
    return redirect('manage_employees')


# ═══════════════════════════════════════════════════════════════════════════════
#  PROMO CODES
# ═══════════════════════════════════════════════════════════════════════════════

@manager_required
def manage_promos(request):
    promos = PromoCode.objects.all().order_by('-created_at')
    return render(request, 'core/manage_promos.html', {'promos': promos})


@manager_required
def add_promo(request):
    if request.method == 'POST':
        code = str(request.POST.get('code', '')).strip().upper()[:30]
        desc = str(request.POST.get('description', '')).strip()[:100]
        dtype = request.POST.get('discount_type', 'percent')
        if dtype not in ('percent', 'fixed'):
            dtype = 'percent'
        dval = request.POST.get('discount_value', '0')
        if not code:
            messages.error(request, "Code is required.")
        elif PromoCode.objects.filter(code=code).exists():
            messages.error(request, f"Code '{code}' already exists.")
        else:
            PromoCode.objects.create(code=code, description=desc,
                                     discount_type=dtype,
                                     discount_value=Decimal(dval or '0'))
            _log(request, 'add_promo', code, detail=f"type={dtype} value={dval}")
            messages.success(request, f"Promo code '{code}' created.")
    return redirect('manage_promos')


@manager_required
def edit_promo(request, promo_id):
    promo = get_object_or_404(PromoCode, id=promo_id)
    if request.method == 'POST':
        promo.code = str(request.POST.get('code', promo.code)).strip().upper()[:30]
        promo.description = str(request.POST.get('description', promo.description)).strip()[:100]
        dtype = request.POST.get('discount_type', promo.discount_type)
        promo.discount_type = dtype if dtype in ('percent', 'fixed') else promo.discount_type
        promo.discount_value = Decimal(request.POST.get('discount_value', str(promo.discount_value)) or '0')
        promo.is_active = request.POST.get('is_active') == 'on'
        promo.save()
        _log(request, 'edit_promo', promo.code)
        messages.success(request, f"Promo '{promo.code}' updated.")
    return redirect('manage_promos')


@manager_required
def delete_promo(request, promo_id):
    promo = get_object_or_404(PromoCode, id=promo_id)
    _log(request, 'delete_promo', promo.code)
    promo.delete()
    messages.success(request, "Promo code deleted.")
    return redirect('manage_promos')


# ═══════════════════════════════════════════════════════════════════════════════
#  EXPENSES
# ═══════════════════════════════════════════════════════════════════════════════

@manager_required
def manage_expenses(request):
    expenses = Expense.objects.filter(is_voided=False).order_by('-date')
    total = sum(e.amount for e in expenses)
    return render(request, 'core/manage_expenses.html', {
        'expenses': expenses, 'total': total,
        'category_choices': Expense.CATEGORY_CHOICES,
    })


@manager_required
def add_expense(request):
    if request.method == 'POST':
        title = str(request.POST.get('title', '')).strip()[:150]
        category = request.POST.get('category', 'other')
        if category not in dict(Expense.CATEGORY_CHOICES):
            category = 'other'
        amount_str = request.POST.get('amount', '0')
        try:
            amount = Decimal(amount_str or '0')
        except InvalidOperation:
            messages.error(request, "Expense amount must be a valid number.")
            return redirect('manage_expenses')
        if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -2:
            messages.error(request, "Expense amount must be greater than zero.")
            return redirect('manage_expenses')
        date_str = request.POST.get('date', '')
        try:
            exp_date = date.fromisoformat(date_str) if date_str else timezone.now().date()
        except ValueError:
            exp_date = timezone.now().date()
        notes = str(request.POST.get('notes', '')).strip()[:1000]

        if title:
            try:
                from accounting.utils import post_expense_journal
                with transaction.atomic():
                    exp = Expense.objects.create(
                        title=title, category=category, amount=amount,
                        date=exp_date, notes=notes,
                    )
                    post_expense_journal(exp, request.user)
            except Exception:
                logger.exception("Failed to record expense and journal")
                messages.error(request, 'Expense could not be recorded. No changes were saved.')
            else:
                _log(request, 'add_expense', title, detail=f"amount={amount} cat={category}")
                messages.success(request, f"Expense '{title}' recorded.")
        else:
            messages.error(request, "Title is required.")

    return redirect('manage_expenses')


@manager_required
def edit_expense(request, expense_id):
    if request.method == 'POST':
        with transaction.atomic():
            exp = get_object_or_404(Expense.objects.select_for_update(), id=expense_id, is_voided=False)
            if (request.POST.get('amount', str(exp.amount)) != str(exp.amount)
                    or request.POST.get('date', exp.date.isoformat()) != exp.date.isoformat()):
                messages.error(request, 'Recorded expense amount and date cannot be changed. Void and re-enter it instead.')
                return redirect('manage_expenses')
            title = str(request.POST.get('title', '')).strip()[:150]
            if not title:
                messages.error(request, 'Title is required.')
                return redirect('manage_expenses')
            exp.title = title
            category = request.POST.get('category', exp.category)
            exp.category = category if category in dict(Expense.CATEGORY_CHOICES) else exp.category
            exp.notes = str(request.POST.get('notes', exp.notes)).strip()[:1000]
            exp.save(update_fields=['title', 'category', 'notes'])
            _log(request, 'edit_expense', exp.title, detail=f"amount={exp.amount}")
            messages.success(request, f"Expense '{exp.title}' updated.")
    return redirect('manage_expenses')


@manager_required
def delete_expense(request, expense_id):
    if request.method != 'POST':
        return redirect('manage_expenses')
    try:
        with transaction.atomic():
            exp = get_object_or_404(Expense.objects.select_for_update(), id=expense_id, is_voided=False)
            from accounting.models import JournalEntry
            from accounting.utils import post_expense_void_journal
            if JournalEntry.objects.filter(source='expense', source_id=exp.pk,
                                           description__startswith='Expense:').exists():
                post_expense_void_journal(exp, request.user)
            exp.is_voided = True
            exp.voided_at = timezone.now()
            exp.save(update_fields=['is_voided', 'voided_at'])
    except Exception:
        logger.exception('Failed to void expense %s', expense_id)
        messages.error(request, 'Expense could not be voided. No changes were saved.')
    else:
        _log(request, 'delete_expense', exp.title, detail=f"voided amount={exp.amount}")
        messages.success(request, "Expense voided.")
    return redirect('manage_expenses')


# ═══════════════════════════════════════════════════════════════════════════════
#  SHIFT REPORT
# ═══════════════════════════════════════════════════════════════════════════════

@manager_required
def shift_report(request):
    _log(request, 'view_shift_report', detail=f"date={request.GET.get('date', 'today')}")
    date_str = request.GET.get('date', timezone.now().date().isoformat())
    try:
        report_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        report_date = timezone.now().date()

    orders = Order.objects.filter(status='active').prefetch_related(
        'items__menu_item').filter(created_at__date=report_date)

    total_revenue = sum(o.total_price for o in orders)
    total_discount = sum(o.discount_amount for o in orders)
    total_orders = orders.count()

    item_summary = {}
    for order in orders:
        order_items = list(order.items.all())
        net_revenues = net_line_revenues(order, order_items)
        for oi in order_items:
            name = oi.menu_item.name if oi.menu_item else '[deleted]'
            if name not in item_summary:
                item_summary[name] = {'qty': 0, 'revenue': Decimal('0')}
            item_summary[name]['qty'] += oi.quantity
            item_summary[name]['revenue'] += net_revenues[oi.pk]

    day_expenses = Expense.objects.filter(date=report_date, is_voided=False)
    total_expenses = sum(e.amount for e in day_expenses)

    monthly_payroll = sum((e.salary for e in Employee.objects.filter(is_active=True)), Decimal('0'))
    daily_payroll = (monthly_payroll / 30).quantize(Decimal('0.01'))

    ingredient_cost = sum((line_cogs(oi) for order in orders for oi in order.items.all()), Decimal('0'))

    net_profit = total_revenue - ingredient_cost - daily_payroll - total_expenses

    return render(request, 'core/shift_report.html', {
        'report_date': report_date, 'orders': orders,
        'total_revenue': total_revenue, 'total_discount': total_discount,
        'total_orders': total_orders,
        'item_summary': item_summary, 'day_expenses': day_expenses,
        'total_expenses': total_expenses, 'daily_payroll': daily_payroll,
        'ingredient_cost': ingredient_cost, 'net_profit': net_profit,
    })


@manager_required
def shift_report_pdf(request):
    date_str = request.GET.get('date', timezone.now().date().isoformat())
    try:
        report_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        report_date = timezone.now().date()

    orders = Order.objects.filter(status='active').prefetch_related(
        'items__menu_item').filter(created_at__date=report_date)

    total_revenue = sum(o.total_price for o in orders)
    total_discount = sum(o.discount_amount for o in orders)
    total_orders = orders.count()

    item_summary = {}
    for order in orders:
        order_items = list(order.items.all())
        net_revenues = net_line_revenues(order, order_items)
        for oi in order_items:
            name = oi.menu_item.name if oi.menu_item else '[deleted]'
            if name not in item_summary:
                item_summary[name] = {'qty': 0, 'revenue': Decimal('0')}
            item_summary[name]['qty'] += oi.quantity
            item_summary[name]['revenue'] += net_revenues[oi.pk]

    day_expenses = Expense.objects.filter(date=report_date, is_voided=False)
    total_expenses = sum(e.amount for e in day_expenses)
    monthly_payroll = sum((e.salary for e in Employee.objects.filter(is_active=True)), Decimal('0'))
    daily_payroll = (monthly_payroll / 30).quantize(Decimal('0.01'))
    ingredient_cost = sum((line_cogs(oi) for order in orders for oi in order.items.all()), Decimal('0'))

    net_profit = total_revenue - ingredient_cost - daily_payroll - total_expenses

    from reportlab.lib import colors as rl_colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable

    _ORANGE = rl_colors.HexColor('#FF4500')
    _LGREY = rl_colors.HexColor('#f3f4f6')
    _MGREY = rl_colors.HexColor('#e5e7eb')
    _GREY = rl_colors.HexColor('#6b7280')
    b8 = ParagraphStyle('b8', fontName='Helvetica-Bold', fontSize=8)
    n8 = ParagraphStyle('n8', fontName='Helvetica', fontSize=8)
    title_st = ParagraphStyle('T', fontName='Helvetica-Bold', fontSize=16, textColor=_ORANGE)
    sub_st = ParagraphStyle('S', fontName='Helvetica', fontSize=9, textColor=_GREY)

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=2 * cm, rightMargin=2 * cm, topMargin=1.5 * cm, bottomMargin=1.5 * cm)
    story = []

    story.append(Paragraph('Searing Sandwiches', title_st))
    story.append(Paragraph(f'Daily Shift Report — {report_date.strftime("%A, %d %B %Y")}', sub_st))
    story.append(Spacer(1, .2 * cm))
    story.append(HRFlowable(width='100%', thickness=1.5, color=_ORANGE))
    story.append(Spacer(1, .4 * cm))

    def _ts():
        return TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), _ORANGE),
            ('TEXTCOLOR', (0, 0), (-1, 0), rl_colors.white),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 0), (-1, -1), 8),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [rl_colors.white, _LGREY]),
            ('GRID', (0, 0), (-1, -1), 0.3, _MGREY),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('LEFTPADDING', (0, 0), (-1, -1), 5),
            ('RIGHTPADDING', (0, 0), (-1, -1), 5),
        ])

    red8 = ParagraphStyle('r8', fontName='Helvetica', fontSize=8,
                          textColor=rl_colors.HexColor('#dc2626'))
    np_color = ParagraphStyle('np', fontName='Helvetica-Bold', fontSize=8,
                              textColor=_ORANGE if net_profit >= 0 else rl_colors.HexColor('#dc2626'))

    kpi_rows = [
        [Paragraph('<b>Metric</b>', b8), Paragraph('<b>Amount (EGP)</b>', b8)],
        [Paragraph('Total Orders', n8), Paragraph(str(total_orders), n8)],
        [Paragraph('Revenue', n8), Paragraph(f'{float(total_revenue):.2f}', n8)],
        [Paragraph('Discounts', n8), Paragraph(f'- {float(total_discount):.2f}', red8)],
        [Paragraph('Ingredient Cost', n8), Paragraph(f'- {float(ingredient_cost):.2f}', red8)],
        [Paragraph('Estimated Daily Payroll', n8), Paragraph(f'- {float(daily_payroll):.2f}', red8)],
        [Paragraph('Day Expenses', n8), Paragraph(f'- {float(total_expenses):.2f}', red8)],
        [Paragraph('<b>Estimated Net Profit</b>', b8),
         Paragraph(f'<b>{float(net_profit):.2f}</b>', np_color)],
    ]
    kt = Table(kpi_rows, colWidths=[8 * cm, 4 * cm])
    kts = _ts()
    kts.add('BACKGROUND', (0, -1), (-1, -1), rl_colors.HexColor('#FFF5F2'))
    kts.add('LINEABOVE', (0, -1), (-1, -1), 1.2, _ORANGE)
    kt.setStyle(kts)
    story.append(kt)
    story.append(Spacer(1, .5 * cm))

    story.append(Paragraph('<b>Sales by Item</b>', b8))
    story.append(Spacer(1, .15 * cm))
    irows = [[Paragraph('<b>Item</b>', b8), Paragraph('<b>Qty</b>', b8),
              Paragraph('<b>Revenue (EGP)</b>', b8)]]
    for name, vals in sorted(item_summary.items(), key=lambda x: -x[1]['revenue']):
        irows.append([Paragraph(name, n8), Paragraph(str(vals['qty']), n8),
                      Paragraph(f'{float(vals["revenue"]):.2f}', n8)])
    it = Table(irows, colWidths=[10 * cm, 2 * cm, 4 * cm], repeatRows=1)
    it.setStyle(_ts())
    story.append(it)

    if day_expenses:
        story.append(Spacer(1, .5 * cm))
        story.append(Paragraph('<b>Day Expenses</b>', b8))
        story.append(Spacer(1, .15 * cm))
        erows = [[Paragraph('<b>Title</b>', b8),
                  Paragraph('<b>Category</b>', b8),
                  Paragraph('<b>Amount</b>', b8)]]
        for exp in day_expenses:
            erows.append([Paragraph(exp.title, n8),
                          Paragraph(exp.get_category_display(), n8),
                          Paragraph(f'{float(exp.amount):.2f}', n8)])
        et = Table(erows, colWidths=[8 * cm, 4 * cm, 4 * cm], repeatRows=1)
        et.setStyle(_ts())
        story.append(et)

    doc.build(story)
    buf.seek(0)
    resp = HttpResponse(buf.read(), content_type='application/pdf')
    resp['Content-Disposition'] = f'attachment; filename="shift_report_{report_date}.pdf"'
    _log(request, 'export_pdf', f'Shift Report {report_date}')
    return resp


# ═══════════════════════════════════════════════════════════════════════════════
#  MENU CATEGORIES
# ═══════════════════════════════════════════════════════════════════════════════

@manager_required
def manage_categories(request):
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'add':
            name = str(request.POST.get('name', '')).strip()[:60]
            sort = request.POST.get('sort_order', 0)
            if name:
                if MenuCategory.objects.filter(name__iexact=name).exists():
                    messages.error(request, f"Category '{name}' already exists.")
                else:
                    MenuCategory.objects.create(name=name, sort_order=sort)
                    _log(request, 'add_category', name)
                    messages.success(request, f"Category '{name}' added.")
            else:
                messages.error(request, "Name is required.")
        elif action == 'edit':
            cat = get_object_or_404(MenuCategory, id=request.POST.get('cat_id'))
            cat.name = str(request.POST.get('name', cat.name)).strip()[:60]
            cat.sort_order = request.POST.get('sort_order', cat.sort_order)
            cat.save()
            _log(request, 'edit_category', cat.name)
            messages.success(request, f"Category '{cat.name}' updated.")
        elif action == 'delete':
            cat = get_object_or_404(MenuCategory, id=request.POST.get('cat_id'))
            name = cat.name;
            cat.delete()
            _log(request, 'delete_category', name)
            messages.success(request, f"Category '{name}' deleted.")
        return redirect('manage_categories')

    categories = MenuCategory.objects.annotate(item_count=Count('items'))
    return render(request, 'core/manage_categories.html', {'categories': categories})


# ═══════════════════════════════════════════════════════════════════════════════
#  STOCK LOG / COST HISTORY
# ═══════════════════════════════════════════════════════════════════════════════

@manager_required
def stock_log_view(request):
    _log(request, 'view_stock_log')
    logs = StockLog.objects.select_related('ingredient', 'changed_by').order_by('-created_at')
    ing_filter = request.GET.get('ingredient', '')
    reason_filter = request.GET.get('reason', '')

    if ing_filter:    logs = logs.filter(ingredient_id=ing_filter)
    if reason_filter: logs = logs.filter(reason=reason_filter)

    paginator = Paginator(logs, 50)
    page_obj = paginator.get_page(request.GET.get('page'))
    return render(request, 'core/stock_log.html', {
        'logs': page_obj,
        'page_obj': page_obj,
        'ingredients': Ingredient.objects.all().order_by('name'),
        'ing_filter': ing_filter,
        'reason_filter': reason_filter,
    })


@manager_required
def cost_history_view(request):
    _log(request, 'view_cost_history')
    ing_id = request.GET.get('ingredient', '')
    history = CostHistory.objects.select_related('ingredient', 'recorded_by').order_by('-created_at')

    if ing_id: history = history.filter(ingredient_id=ing_id)

    paginator = Paginator(history, 50)
    page_obj = paginator.get_page(request.GET.get('page'))
    return render(request, 'core/cost_history.html', {
        'history': page_obj,
        'page_obj': page_obj,
        'ingredients': Ingredient.objects.all().order_by('name'),
        'ing_filter': ing_id,
    })


# ═══════════════════════════════════════════════════════════════════════════════
#  EXCEL EXPORTS  (core module)
# ═══════════════════════════════════════════════════════════════════════════════

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

_ORANGE_HEX = 'FF4500'
_LIGHT_HEX = 'FFF5F2'
_HEADER_FONT = Font(bold=True, color='FFFFFF', size=10)
_HEADER_FILL = PatternFill('solid', fgColor=_ORANGE_HEX)
_TOTAL_FILL = PatternFill('solid', fgColor=_LIGHT_HEX)
_TOTAL_FONT = Font(bold=True, color=_ORANGE_HEX, size=10)
_BORDER = Border(
    left=Side(style='thin', color='E5E7EB'), right=Side(style='thin', color='E5E7EB'),
    top=Side(style='thin', color='E5E7EB'), bottom=Side(style='thin', color='E5E7EB'),
)
_CENTER = Alignment(horizontal='center', vertical='center')
_LEFT = Alignment(horizontal='left', vertical='center')
_RIGHT = Alignment(horizontal='right', vertical='center')


def _xl_response(wb, filename):
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    resp = HttpResponse(buf, content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    resp['Content-Disposition'] = f'attachment; filename="{filename}"'
    return resp


def _xl_header_row(ws, cols):
    for ci, (title, width) in enumerate(cols, 1):
        c = ws.cell(row=1, column=ci, value=title)
        c.font = _HEADER_FONT;
        c.fill = _HEADER_FILL
        c.alignment = _CENTER;
        c.border = _BORDER
        ws.column_dimensions[get_column_letter(ci)].width = width
    ws.row_dimensions[1].height = 20


def _xl_data_cell(ws, row, col, value, num_fmt=None, bold=False, fill=None, align=None):
    c = ws.cell(row=row, column=col, value=value)
    c.border = _BORDER
    c.alignment = align or _LEFT
    if num_fmt: c.number_format = num_fmt
    if bold:    c.font = Font(bold=True)
    if fill:    c.fill = fill
    return c


@manager_required
def excel_report_financial(request):
    orders, date_from_str, date_to_str = _get_filtered_orders(request)
    _log(request, 'export_excel', 'Financial Report',
         detail=f"from={date_from_str} to={date_to_str}")

    wb = Workbook()
    ws = wb.active
    ws.title = 'Financial Report'
    ws.row_dimensions[1].height = 26
    ws.row_dimensions[2].height = 18
    title_c = ws.cell(row=1, column=1,
                      value=f'Searing Sandwiches — Financial Report  |  {_period_label(date_from_str, date_to_str)}')
    title_c.font = Font(bold=True, size=13, color=_ORANGE_HEX)
    title_c.alignment = _LEFT

    cols = [('Order #', 13), ('Date', 12), ('Time', 8), ('Product', 28),
            ('Unit Cost', 12), ('Sale Price', 12), ('Qty', 6),
            ('Full Cost', 13), ('Revenue', 13), ('Profit', 13)]

    for ci, (title, width) in enumerate(cols, 1):
        c = ws.cell(row=2, column=ci, value=title)
        c.font = _HEADER_FONT;
        c.fill = _HEADER_FILL
        c.alignment = _CENTER;
        c.border = _BORDER
        ws.column_dimensions[get_column_letter(ci)].width = width

    r = 3
    grand_cost = grand_revenue = grand_profit = 0.0
    for order in orders:
        order_items = list(order.items.all())
        net_revenues = net_line_revenues(order, order_items)
        for oi in order_items:
            qty = oi.quantity;
            fc = float(line_cogs(oi))
            unit_cost = fc / qty if qty else 0
            sale = float(oi.price_at_order)
            fr = float(net_revenues[oi.pk])
            prof = fr - fc
            grand_cost += fc;
            grand_revenue += fr;
            grand_profit += prof
            for ci, v in enumerate([
                order.order_number,
                order.created_at.strftime('%Y-%m-%d'),
                order.created_at.strftime('%H:%M'),
                oi.menu_item.name if oi.menu_item else '[Deleted menu item]',
                round(unit_cost, 2), round(sale, 2), qty,
                round(fc, 2), round(fr, 2), round(prof, 2)
            ], 1):
                fmt = '#,##0.00' if ci in (5, 6, 8, 9, 10) else None
                aln = _RIGHT if ci in (5, 6, 7, 8, 9, 10) else _LEFT
                _xl_data_cell(ws, r, ci, v, num_fmt=fmt, align=aln)
            r += 1

    for ci, v in enumerate([
        'TOTAL', '', '', '', '', '', '',
        round(grand_cost, 2), round(grand_revenue, 2), round(grand_profit, 2)
    ], 1):
        c = _xl_data_cell(ws, r, ci, v, num_fmt='#,##0.00' if ci >= 8 else None,
                          bold=True, fill=_TOTAL_FILL, align=_RIGHT if ci >= 8 else _CENTER)
        if ci >= 8: c.font = _TOTAL_FONT

    ws2 = wb.create_sheet('Summary')
    ws2.column_dimensions['A'].width = 28
    ws2.column_dimensions['B'].width = 18
    from accounting.reporting import profit_loss_totals
    pnl = profit_loss_totals(date_from_str, date_to_str)
    net_profit = float(pnl['net_profit'])
    summary = [
        ('Posted Sales Revenue', float(pnl['sales'])),
        ('Posted COGS', float(pnl['cogs'])),
        ('Posted Gross Profit', float(pnl['gross_profit'])),
        ('Other Revenue', float(pnl['other_revenue'])),
        ('Payroll Posted', -float(pnl['payroll'])),
        ('Other Expenses incl. Damage', -float(pnl['operating'] + pnl['damage'] + pnl['other_expenses'])),
        ('Net Profit', net_profit),
    ]
    ws2['A1'].value = 'Summary'
    ws2['A1'].font = Font(bold=True, size=12, color=_ORANGE_HEX)
    for i, (label, val) in enumerate(summary, 2):
        c1 = ws2.cell(row=i, column=1, value=label)
        c2 = ws2.cell(row=i, column=2, value=round(val, 2))
        c1.border = _BORDER;
        c2.border = _BORDER
        c2.number_format = '#,##0.00';
        c2.alignment = _RIGHT
        if label == 'Net Profit':
            c1.font = Font(bold=True);
            c2.font = Font(bold=True, color=_ORANGE_HEX)
            c1.fill = _TOTAL_FILL;
            c2.fill = _TOTAL_FILL

    return _xl_response(wb, 'financial_report.xlsx')


@manager_required
def excel_report_ingredients(request):
    _log(request, 'export_excel', 'Ingredient Usage Report',
         detail=f"from={request.GET.get('date_from', '')} to={request.GET.get('date_to', '')}")
    orders, date_from_str, date_to_str = _get_filtered_orders(request)
    item_qty = {};
    item_names = {}
    for order in orders:
        for oi in order.items.all():
            if not oi.menu_item: continue
            mid = oi.menu_item_id
            item_qty[mid] = item_qty.get(mid, 0) + oi.quantity
            item_names[mid] = oi.menu_item

    wb = Workbook()
    ws = wb.active
    ws.title = 'Ingredient Usage'
    ws.row_dimensions[1].height = 26
    ws.cell(row=1, column=1,
            value=f'Searing Sandwiches — Ingredient Usage  |  {_period_label(date_from_str, date_to_str)}'
            ).font = Font(bold=True, size=13, color=_ORANGE_HEX)

    cols = [('Product Name', 28), ('Qty Sold', 10), ('Ingredient', 22),
            ('Qty Used', 12), ('Unit', 10), ('Ing. Cost (EGP)', 16)]
    for ci, (title, width) in enumerate(cols, 1):
        c = ws.cell(row=3, column=ci, value=title)
        c.font = _HEADER_FONT;
        c.fill = _HEADER_FILL
        c.alignment = _CENTER;
        c.border = _BORDER
        ws.column_dimensions[get_column_letter(ci)].width = width

    r = 4
    for mid, menu_item in item_names.items():
        qty_sold = item_qty[mid]
        recipes = list(menu_item.ingredients.all())
        if not recipes:
            for ci, v in enumerate([menu_item.name, qty_sold, '—', '—', '—', '—'], 1):
                _xl_data_cell(ws, r, ci, v)
            r += 1
            continue
        for i, recipe in enumerate(recipes):
            ing = recipe.ingredient
            sq = float(_convert_to_stock_unit(recipe.quantity_required, recipe.recipe_unit, ing.unit))
            tqu = round(sq * qty_sold, 3)
            ic = round(tqu * float(ing.cost_per_unit), 2)
            for ci, v in enumerate([
                menu_item.name if i == 0 else '',
                qty_sold if i == 0 else '',
                ing.name, tqu, ing.unit, ic
            ], 1):
                fmt = '#,##0.000' if ci == 4 else ('#,##0.00' if ci == 6 else None)
                aln = _RIGHT if ci in (2, 4, 6) else _LEFT
                _xl_data_cell(ws, r, ci, v, num_fmt=fmt, align=aln)
            r += 1
        ws.row_dimensions[r].height = 6;
        r += 1

    return _xl_response(wb, 'ingredient_usage.xlsx')


@manager_required
def excel_report_sales(request):
    _log(request, 'export_excel', 'Sales Summary',
         detail=f"from={request.GET.get('date_from', '')} to={request.GET.get('date_to', '')}")
    date_from_str = request.GET.get('date_from', '')
    date_to_str = request.GET.get('date_to', '')
    orders = Order.objects.filter(status='active').prefetch_related(
        'items__menu_item__ingredients__ingredient').order_by('created_at')

    if date_from_str:
        try:
            orders = orders.filter(
                created_at__date__gte=datetime.strptime(date_from_str, '%Y-%m-%d').date())
        except ValueError:
            pass
    if date_to_str:
        try:
            orders = orders.filter(
                created_at__date__lte=datetime.strptime(date_to_str, '%Y-%m-%d').date())
        except ValueError:
            pass

    wb = Workbook()
    ws_orders = wb.active
    ws_orders.title = 'Orders'
    ws_orders.row_dimensions[1].height = 26
    ws_orders.cell(row=1, column=1,
                   value=f'Searing Sandwiches — Orders  |  {_period_label(date_from_str, date_to_str)}'
                   ).font = Font(bold=True, size=13, color=_ORANGE_HEX)

    o_cols = [('Order #', 14), ('Date', 13), ('Time', 8), ('Table', 8),
              ('Discount', 12), ('Total', 14), ('Status', 10)]
    for ci, (t, w) in enumerate(o_cols, 1):
        c = ws_orders.cell(row=3, column=ci, value=t)
        c.font = _HEADER_FONT;
        c.fill = _HEADER_FILL;
        c.alignment = _CENTER;
        c.border = _BORDER
        ws_orders.column_dimensions[get_column_letter(ci)].width = w

    r = 4
    grand_total = Decimal('0')
    for order in orders:
        grand_total += order.total_price
        for ci, v in enumerate([
            order.order_number,
            order.created_at.strftime('%Y-%m-%d'),
            order.created_at.strftime('%H:%M'),
            order.table_number or '',
            float(order.discount_amount),
            float(order.total_price),
            order.status
        ], 1):
            fmt = '#,##0.00' if ci in (5, 6) else None
            aln = _RIGHT if ci in (5, 6) else _LEFT
            _xl_data_cell(ws_orders, r, ci, v, num_fmt=fmt, align=aln)
        r += 1

    for ci, v in enumerate(['GRAND TOTAL', '', '', '', '', float(grand_total), ''], 1):
        c = _xl_data_cell(ws_orders, r, ci, v,
                          num_fmt='#,##0.00' if ci == 6 else None,
                          bold=True, fill=_TOTAL_FILL, align=_RIGHT if ci == 6 else _CENTER)
        if ci == 6: c.font = _TOTAL_FONT

    ws_items = wb.create_sheet('Top Items')
    item_stats = {}
    for order in orders:
        order_items = list(order.items.all())
        net_revenues = net_line_revenues(order, order_items)
        for oi in order_items:
            n = oi.menu_item.name if oi.menu_item else '[Deleted menu item]'
            if n not in item_stats:
                item_stats[n] = {'qty': 0, 'revenue': Decimal('0'), 'cost': Decimal('0')}
            item_stats[n]['qty'] += oi.quantity
            item_stats[n]['revenue'] += net_revenues[oi.pk]
            item_stats[n]['cost'] += line_cogs(oi)

    ws_items.row_dimensions[1].height = 26
    ws_items.cell(row=1, column=1,
                  value='Sales by Menu Item').font = Font(bold=True, size=13, color=_ORANGE_HEX)
    i_cols = [('Item', 28), ('Qty Sold', 10), ('Revenue (EGP)', 16), ('COGS (EGP)', 14), ('Margin %', 12)]

    for ci, (t, w) in enumerate(i_cols, 1):
        c = ws_items.cell(row=3, column=ci, value=t)
        c.font = _HEADER_FONT;
        c.fill = _HEADER_FILL;
        c.alignment = _CENTER;
        c.border = _BORDER
        ws_items.column_dimensions[get_column_letter(ci)].width = w

    for ri, (name, d) in enumerate(sorted(item_stats.items(), key=lambda x: -x[1]['qty']), 4):
        rev = float(d['revenue'])
        cost = float(d['cost'])
        margin = round((rev - cost) / rev * 100, 1) if rev else 0
        for ci, v in enumerate([name, d['qty'], round(rev, 2), round(cost, 2), margin], 1):
            fmt = '#,##0.00' if ci in (3, 4) else ('0.0"%"' if ci == 5 else None)
            aln = _RIGHT if ci > 1 else _LEFT
            _xl_data_cell(ws_items, ri, ci, v, num_fmt=fmt, align=aln)

    return _xl_response(wb, 'sales_report.xlsx')


@manager_required
def excel_stock_log(request):
    _log(request, 'export_excel', 'Stock Log')
    logs = StockLog.objects.select_related('ingredient', 'changed_by').order_by('-created_at')[:1000]
    wb = Workbook()
    ws = wb.active
    ws.title = 'Stock Log'
    ws.row_dimensions[1].height = 26
    ws.cell(row=1, column=1,
            value='Searing Sandwiches — Stock Log').font = Font(bold=True, size=13, color=_ORANGE_HEX)

    cols = [('Date/Time', 18), ('Ingredient', 22), ('Reason', 14),
            ('Before', 12), ('Change', 12), ('After', 12), ('By', 16)]
    for ci, (t, w) in enumerate(cols, 1):
        c = ws.cell(row=3, column=ci, value=t)
        c.font = _HEADER_FONT;
        c.fill = _HEADER_FILL;
        c.alignment = _CENTER;
        c.border = _BORDER
        ws.column_dimensions[get_column_letter(ci)].width = w

    for ri, log in enumerate(logs, 4):
        for ci, v in enumerate([
            log.created_at.strftime('%Y-%m-%d %H:%M'),
            log.ingredient.name if log.ingredient else '—',
            log.get_reason_display(),
            float(log.qty_before), float(log.qty_change), float(log.qty_after),
            log.changed_by.username if log.changed_by else '—',
        ], 1):
            fmt = '#,##0.000' if ci in (4, 5, 6) else None
            aln = _RIGHT if ci in (4, 5, 6) else _LEFT
            _xl_data_cell(ws, ri, ci, v, num_fmt=fmt, align=aln)

    return _xl_response(wb, 'stock_log.xlsx')


@manager_required
def excel_cost_history(request):
    _log(request, 'export_excel', 'Cost History')
    history = CostHistory.objects.select_related('ingredient', 'recorded_by').order_by('-created_at')[:1000]
    wb = Workbook()
    ws = wb.active
    ws.title = 'Cost History'
    ws.row_dimensions[1].height = 26
    ws.cell(row=1, column=1,
            value='Searing Sandwiches — Cost History').font = Font(bold=True, size=13, color=_ORANGE_HEX)

    cols = [('Date/Time', 18), ('Ingredient', 22), ('Old Cost', 12), ('New Cost', 12),
            ('Qty Restocked', 15), ('Total Paid', 13), ('By', 16)]
    for ci, (t, w) in enumerate(cols, 1):
        c = ws.cell(row=3, column=ci, value=t)
        c.font = _HEADER_FONT;
        c.fill = _HEADER_FILL;
        c.alignment = _CENTER;
        c.border = _BORDER
        ws.column_dimensions[get_column_letter(ci)].width = w

    for ri, h in enumerate(history, 4):
        for ci, v in enumerate([
            h.created_at.strftime('%Y-%m-%d %H:%M'),
            h.ingredient.name if h.ingredient else '—',
            float(h.old_cost), float(h.new_cost),
            float(h.qty_restocked), float(h.total_paid),
            h.recorded_by.username if h.recorded_by else '—',
        ], 1):
            fmt = '#,##0.0000' if ci in (3, 4) else ('#,##0.000' if ci == 5 else ('#,##0.00' if ci == 6 else None))
            aln = _RIGHT if ci in (3, 4, 5, 6) else _LEFT
            _xl_data_cell(ws, ri, ci, v, num_fmt=fmt, align=aln)

    return _xl_response(wb, 'cost_history.xlsx')


@manager_required
def excel_activity_log(request):
    _log(request, 'export_excel', 'Activity Log')
    logs = ActivityLog.objects.select_related('user').order_by('-created_at')[:2000]
    wb = Workbook()
    ws = wb.active
    ws.title = 'Activity Log'
    ws.row_dimensions[1].height = 26
    ws.cell(row=1, column=1,
            value='Searing Sandwiches — Activity Log').font = Font(bold=True, size=13, color=_ORANGE_HEX)

    cols = [('Date/Time', 18), ('User', 16), ('Action', 24), ('Target', 28), ('Detail', 36), ('IP Address', 16)]
    for ci, (t, w) in enumerate(cols, 1):
        c = ws.cell(row=3, column=ci, value=t)
        c.font = _HEADER_FONT;
        c.fill = _HEADER_FILL;
        c.alignment = _CENTER;
        c.border = _BORDER
        ws.column_dimensions[get_column_letter(ci)].width = w

    for ri, log in enumerate(logs, 4):
        for ci, v in enumerate([
            log.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            log.user.username if log.user else '—',
            log.get_action_display(),
            log.target or '—',
            log.detail or '',
            log.ip_address or '—',
        ], 1):
            _xl_data_cell(ws, ri, ci, v)

    return _xl_response(wb, 'activity_log.xlsx')


# ═══════════════════════════════════════════════════════════════════════════════
#  FULL DATA BACKUP
# ═══════════════════════════════════════════════════════════════════════════════

@manager_required
def export_full_backup(request):
    wb = Workbook()
    HDR_FONT = Font(bold=True, color='FFFFFF', size=9)
    HDR_FILL = PatternFill('solid', fgColor='FF4500')
    HDR_AL = Alignment(horizontal='center', vertical='center')

    def _sheet(name, headers, rows):
        ws = wb.create_sheet(name)
        ws.append(headers)
        for ci, h in enumerate(headers, 1):
            c = ws.cell(row=1, column=ci)
            c.font = HDR_FONT;
            c.fill = HDR_FILL;
            c.alignment = HDR_AL
            ws.column_dimensions[get_column_letter(ci)].width = max(14, len(str(h)) + 4)
        for row in rows:
            ws.append([str(v) if v is not None else '' for v in row])

    _sheet('Orders',
           ['ID', 'Order#', 'Date', 'Total EGP', 'Discount EGP', 'Status', 'Void Reason', 'Promo'],
           [(o.id, o.order_number, o.created_at.strftime('%Y-%m-%d %H:%M'),
             float(o.total_price), float(o.discount_amount), o.status,
             o.void_reason, o.promo_code.code if o.promo_code else '')
            for o in Order.objects.select_related('promo_code').order_by('-created_at')])

    _sheet('OrderItems',
           ['ID', 'Order#', 'Menu Item', 'Qty', 'Price At Order EGP', 'COGS At Sale EGP'],
           [(oi.id, oi.order.order_number,
             oi.menu_item.name if oi.menu_item else '[deleted]',
             oi.quantity, float(oi.price_at_order), float(getattr(oi, 'cogs_at_sale', 0)))
            for oi in OrderItem.objects.select_related('order', 'menu_item').order_by('-order__created_at')])

    _sheet('MenuItems',
           ['ID', 'Name', 'Category', 'Price EGP', 'Active', 'Description'],
           [(m.id, m.name, m.category.name if m.category else '',
             float(m.price), m.is_active, m.description)
            for m in MenuItem.objects.select_related('category').order_by('name')])

    _sheet('Ingredients',
           ['ID', 'Name', 'Unit', 'Stock Qty', 'Cost/Unit EGP', 'Low Stock Threshold'],
           [(i.id, i.name, i.unit, float(i.stock_quantity),
             float(i.cost_per_unit), float(i.low_stock_threshold))
            for i in Ingredient.objects.order_by('name')])

    _sheet('Expenses',
           ['ID', 'Date', 'Title', 'Category', 'Amount EGP', 'Notes'],
           [(e.id, str(e.date), e.title, e.get_category_display(),
             float(e.amount), e.notes)
            for e in Expense.objects.filter(is_voided=False).order_by('-date')])

    _sheet('Employees',
           ['ID', 'Full Name', 'Job', 'Phone', 'Salary EGP', 'Active', 'Hire Date'],
           [(e.id, e.full_name, e.get_job_title_display(), e.phone,
             float(e.salary), e.is_active, str(e.hire_date))
            for e in Employee.objects.order_by('full_name')])

    _sheet('StockLog',
           ['ID', 'Date', 'Ingredient', 'Reason', 'Before', 'Change', 'After', 'Note', 'By'],
           [(l.id, l.created_at.strftime('%Y-%m-%d %H:%M'),
             l.ingredient.name if l.ingredient else '—',
             l.get_reason_display(), float(l.qty_before), float(l.qty_change),
             float(l.qty_after), l.note,
             l.changed_by.username if l.changed_by else '')
            for l in StockLog.objects.select_related('ingredient', 'changed_by').order_by('-created_at')])

    _sheet('CostHistory',
           ['ID', 'Date', 'Ingredient', 'Old Cost', 'New Cost', 'Qty Restocked', 'Total Paid EGP', 'By'],
           [(h.id, h.created_at.strftime('%Y-%m-%d %H:%M'),
             h.ingredient.name if h.ingredient else '—',
             float(h.old_cost), float(h.new_cost), float(h.qty_restocked),
             float(h.total_paid), h.recorded_by.username if h.recorded_by else '')
            for h in CostHistory.objects.select_related('ingredient', 'recorded_by').order_by('-created_at')])

    _sheet('ActivityLog',
           ['ID', 'Date', 'User', 'Action', 'Target', 'Detail', 'IP'],
           [(l.id, l.created_at.strftime('%Y-%m-%d %H:%M'),
             l.user.username if l.user else '',
             l.get_action_display(), l.target, l.detail, l.ip_address or '')
            for l in ActivityLog.objects.select_related('user').order_by('-created_at')])

    if 'Sheet' in wb.sheetnames:
        del wb['Sheet']

    _log(request, 'export_excel', 'Full Data Backup')
    stamp = timezone.now().strftime('%Y%m%d_%H%M')
    return _xl_response(wb, f'searing_backup_{stamp}.xlsx')


# ═══════════════════════════════════════════════════════════════════════════════
#  ACTIVITY LOG VIEW
# ═══════════════════════════════════════════════════════════════════════════════

@manager_required
def activity_log_view(request):
    logs = ActivityLog.objects.select_related('user').order_by('-created_at')
    user_filter = request.GET.get('user', '')[:50]
    action_filter = request.GET.get('action', '')
    date_filter = request.GET.get('date', '')

    if user_filter:   logs = logs.filter(user__username__icontains=user_filter)
    if action_filter: logs = logs.filter(action=action_filter)
    if date_filter:
        try:
            logs = logs.filter(
                created_at__date=datetime.strptime(date_filter, '%Y-%m-%d').date())
        except ValueError:
            pass

    paginator = Paginator(logs, 50)
    page_obj = paginator.get_page(request.GET.get('page'))
    return render(request, 'core/activity_log.html', {
        'logs': page_obj,
        'page_obj': page_obj,
        'action_choices': ActivityLog.ACTION_CHOICES,
        'user_filter': user_filter,
        'action_filter': action_filter,
        'date_filter': date_filter,
    })
