from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
from django.http import JsonResponse
from django.db import transaction
from django.db.models import Sum, F, Count
from django.utils import timezone
from django.contrib import messages
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.models import User
from .models import Ingredient, MenuItem, MenuItemIngredient, Order, OrderItem, UserProfile, Employee, PromoCode, Expense, MenuCategory, StockLog, CostHistory, ActivityLog, DamagedStock
from django.core.mail import send_mail
from django.conf import settings as django_settings


def _log(request, action, target='', detail=''):
    """Create an ActivityLog entry. Safe to call from anywhere."""
    try:
        ip = request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip() \
             or request.META.get('REMOTE_ADDR')
        ActivityLog.objects.create(
            user=request.user if request.user.is_authenticated else None,
            action=action, target=target, detail=detail,
            ip_address=ip or None,
        )
    except Exception:
        pass  # Never let logging break the actual view

# Stock unit → allowed recipe units
# For boxes the allowed unit comes from ingredient.sub_unit (cans or packets)
UNIT_FAMILIES = {
    'kg':     ['g'],
    'L':      ['mL'],
    'pieces': ['pieces'],
    # boxes handled separately via ingredient.sub_unit
}

def _allowed_recipe_units(ingredient):
    """Return list of valid recipe units for an ingredient."""
    if ingredient.unit == 'boxes':
        return [ingredient.sub_unit]
    return UNIT_FAMILIES.get(ingredient.unit, ['pieces'])

def _validate_recipe_unit(ingredient, recipe_unit):
    """Return True if recipe_unit is valid for this ingredient."""
    return recipe_unit in _allowed_recipe_units(ingredient)

def _convert_to_stock_unit(qty, recipe_unit, stock_unit):
    """Convert recipe quantity to stock unit for deduction & cost calc.
    For boxes, stock_quantity is stored in sub_units (cans/packets), so 1:1.
    """
    from decimal import Decimal
    qty = Decimal(str(qty))
    if recipe_unit == 'g'  and stock_unit == 'kg': return qty / Decimal('1000')
    if recipe_unit == 'mL' and stock_unit == 'L':  return qty / Decimal('1000')
    # pieces->pieces, cans->boxes(stored as cans), packets->boxes(stored as packets) all 1:1
    return qty


def _log_stock(ingredient, qty_change, reason, user=None, note=''):
    """Record a stock movement in StockLog."""
    before = ingredient.stock_quantity
    StockLog.objects.create(
        ingredient=ingredient,
        changed_by=user,
        reason=reason,
        qty_before=before,
        qty_change=qty_change,
        qty_after=before + qty_change,
        note=note,
    )


def _send_low_stock_alert(ingredient):
    """Email all managers when an ingredient drops below its threshold — runs in background thread."""
    import threading
    def _send():
        try:
            manager_emails = list(
                User.objects.filter(profile__role__in=('manager','superadmin'), email__isnull=False)
                .exclude(email='').values_list('email', flat=True)
            )
            if not manager_emails:
                return
            subject = f"[Searing Sandwiches] Low Stock Alert: {ingredient.name}"
            body = (
                f"Stock Alert\n\n"
                f"Ingredient: {ingredient.name}\n"
                f"Current Stock: {ingredient.stock_quantity} {ingredient.unit}\n"
                f"Threshold: {ingredient.low_stock_threshold} {ingredient.unit}\n\n"
                f"Please restock soon.\n\n"
                f"— Searing Sandwiches POS"
            )
            send_mail(subject, body, django_settings.DEFAULT_FROM_EMAIL, manager_emails, fail_silently=True)
        except Exception:
            pass
    threading.Thread(target=_send, daemon=True).start()
import json
from datetime import datetime, date

def is_superadmin(user):
    return user.is_authenticated and hasattr(user, 'profile') and user.profile.role == 'superadmin'

def is_manager(user):
    return user.is_authenticated and hasattr(user, 'profile') and user.profile.role in ('manager', 'superadmin')

def is_cashier(user):
    return user.is_authenticated and hasattr(user, 'profile') and user.profile.role == 'cashier'

def manager_required(view_func):
    return user_passes_test(is_manager, login_url='login')(view_func)

def superadmin_required(view_func):
    return user_passes_test(is_superadmin, login_url='login')(view_func)

def cashier_or_manager_required(view_func):
    return user_passes_test(lambda u: is_manager(u) or is_cashier(u), login_url='login')(view_func)

def login_view(request):
    if request.method == 'POST':
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            _log(request, 'login', user.username)
            if is_manager(user):
                return redirect('dashboard')
            return redirect('pos')
    else:
        form = AuthenticationForm()
    return render(request, 'core/login.html', {'form': form})

def logout_view(request):
    _log(request, 'logout', request.user.username if request.user.is_authenticated else '')
    logout(request)
    return redirect('login')

def home(request):
    if request.user.is_authenticated:
        if is_manager(request.user):
            return redirect('dashboard')
        return redirect('pos')
    return render(request, "core/index.html")

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
    if request.method == "POST":
        try:
            from decimal import Decimal
            data = json.loads(request.body)
            cart        = data.get("cart", [])
            notes       = data.get("notes", "")
            table_number= data.get("table_number", "").strip()
            promo_code_str = data.get("promo_code", "").strip().upper()
            if not cart:
                return JsonResponse({"success": False, "error": "Cart is empty"}, status=400)

            # Validate promo code
            promo_obj      = None
            discount_amount = Decimal("0.00")
            if promo_code_str:
                try:
                    promo_obj = PromoCode.objects.get(code__iexact=promo_code_str, is_active=True)
                except PromoCode.DoesNotExist:
                    return JsonResponse({"success": False, "error": f"Promo code '{promo_code_str}' is invalid or inactive."}, status=400)

            with transaction.atomic():
                order = Order.objects.create(customer_notes=notes, table_number=table_number)
                subtotal = Decimal("0.00")

                # Collect all menu item IDs and prefetch everything in 2 queries
                item_ids = [item["id"] for item in cart]
                menu_items_qs = MenuItem.objects.filter(id__in=item_ids).prefetch_related(
                    'ingredients__ingredient'
                )
                menu_items_map = {mi.id: mi for mi in menu_items_qs}

                for item in cart:
                    menu_item = menu_items_map.get(item["id"])
                    if not menu_item:
                        raise Exception(f"Menu item {item['id']} not found")
                    quantity  = int(item["quantity"])
                    # Snapshot COGS at current ingredient costs before deducting stock
                    cogs_unit = Decimal('0')
                    for recipe_item in menu_item.ingredients.all():
                        recipe_qty   = recipe_item.quantity_required * quantity
                        required_qty = _convert_to_stock_unit(recipe_qty, recipe_item.recipe_unit, recipe_item.ingredient.unit)
                        ingredient   = Ingredient.objects.select_for_update().get(id=recipe_item.ingredient.id)
                        cogs_unit   += required_qty * ingredient.cost_per_unit
                        if ingredient.stock_quantity < required_qty:
                            raise Exception(f"Insufficient stock for {ingredient.name}")
                        before = ingredient.stock_quantity
                        ingredient.stock_quantity -= required_qty
                        ingredient.save()
                        _log_stock(ingredient, -required_qty, 'sale', request.user,
                                   note=f'Order {order.order_number}')
                        if ingredient.stock_quantity <= ingredient.low_stock_threshold:
                            _send_low_stock_alert(ingredient)
                    try:
                        OrderItem.objects.create(
                            order=order, menu_item=menu_item,
                            quantity=quantity, price_at_order=menu_item.price,
                            cogs_at_sale=cogs_unit,
                        )
                        # Invalidate cached column check so it picks up after migration
                        from .models import _SafeOrderItemManager
                        _SafeOrderItemManager._col_exists = None
                    except Exception as _oi_err:
                        if 'cogs_at_sale' in str(_oi_err):
                            # Column not yet created — create without it (pre-migration fallback)
                            OrderItem.objects.create(
                                order=order, menu_item=menu_item,
                                quantity=quantity, price_at_order=menu_item.price,
                            )
                        else:
                            raise
                    subtotal += menu_item.price * quantity

                if promo_obj:
                    if promo_obj.discount_type == 'percent':
                        discount_amount = (subtotal * promo_obj.discount_value / Decimal("100")).quantize(Decimal("0.01"))
                    else:
                        discount_amount = min(promo_obj.discount_value, subtotal)
                    order.promo_code     = promo_obj
                    order.discount_amount = discount_amount
                    # Increment usage counter
                    PromoCode.objects.filter(id=promo_obj.id).update(times_used=F('times_used') + 1)

                order.total_price = subtotal - discount_amount
                order.save()
            # Post accounting journal (non-blocking)
            try:
                from accounting.utils import post_sale_journal
                post_sale_journal(order, request.user)
            except Exception:
                pass
            _log(request, 'place_order', order.order_number,
                 detail=f"total={float(order.total_price):.2f} EGP, {len(cart)} item(s)")
            return JsonResponse({
                "success": True,
                "order_number": order.order_number,
                "discount": float(discount_amount),
                "total": float(order.total_price),
            })
        except Exception as e:
            return JsonResponse({"success": False, "error": str(e)}, status=400)
    return JsonResponse({"success": False, "error": "Invalid request"}, status=405)


@cashier_or_manager_required
def validate_promo(request):
    """AJAX: check promo code and return discount info."""
    if request.method == "POST":
        from decimal import Decimal
        data = json.loads(request.body)
        code = data.get("code", "").strip().upper()
        subtotal = Decimal(str(data.get("subtotal", "0")))
        try:
            from django.utils import timezone as _tz
            promo = PromoCode.objects.get(code__iexact=code, is_active=True)
            today = _tz.now().date()
            if promo.valid_from and today < promo.valid_from:
                return JsonResponse({"success": False, "error": f"Promo code is not valid yet (starts {promo.valid_from})."}, status=400)
            if promo.valid_until and today > promo.valid_until:
                return JsonResponse({"success": False, "error": f"Promo code expired on {promo.valid_until}."}, status=400)
            if promo.max_uses and promo.times_used >= promo.max_uses:
                return JsonResponse({"success": False, "error": "Promo code has reached its maximum number of uses."}, status=400)
            if promo.discount_type == 'percent':
                discount = (subtotal * promo.discount_value / Decimal("100")).quantize(Decimal("0.01"))
                label = f"{promo.discount_value}% off"
            else:
                discount = min(promo.discount_value, subtotal)
                label = f"{promo.discount_value} EGP off"
            return JsonResponse({"valid": True, "discount": float(discount), "label": label, "description": promo.description, "type": promo.discount_type, "raw_value": float(promo.discount_value)})
        except PromoCode.DoesNotExist:
            return JsonResponse({"valid": False, "error": "Invalid or inactive promo code"})
    return JsonResponse({"valid": False, "error": "Bad request"}, status=405)

@cashier_or_manager_required
def receipt_view(request, order_number):
    order = get_object_or_404(Order, order_number=order_number)
    return render(request, "core/receipt.html", {"order": order})

@manager_required
def dashboard_view(request):
    _log(request, 'view_dashboard')
    from decimal import Decimal
    import json as _json

    ingredients  = Ingredient.objects.all()
    orders       = Order.objects.filter(status='active').prefetch_related(
        'items__menu_item').order_by('-created_at')[:50]
    total_sales  = Order.objects.filter(status='active').aggregate(
        Sum('total_price'))['total_price__sum'] or 0

    # Low stock alerts
    low_stock = [i for i in ingredients if i.stock_quantity <= i.low_stock_threshold]

    # Hourly heatmap — last 30 days, active orders only
    from django.db.models.functions import ExtractHour
    thirty_days_ago = timezone.now() - timezone.timedelta(days=30)
    hourly = (Order.objects
              .filter(status='active', created_at__gte=thirty_days_ago)
              .annotate(hour=ExtractHour('created_at'))
              .values('hour')
              .annotate(cnt=Count('id'))
              .order_by('hour'))
    hour_data  = {h: 0 for h in range(24)}
    for row in hourly:
        hour_data[row['hour']] = row['cnt']
    chart_hours  = list(range(24))
    chart_counts = [hour_data[h] for h in chart_hours]

    # Break-even
    monthly_payroll  = sum((e.salary for e in Employee.objects.filter(is_active=True)), Decimal('0'))
    monthly_expenses = sum((e.amount for e in Expense.objects.filter(
        date__month=timezone.now().month, date__year=timezone.now().year)), Decimal('0'))
    fixed_costs = monthly_payroll + monthly_expenses

    # Average order value & margin — last 30 days
    recent_orders = Order.objects.filter(status='active', created_at__gte=thirty_days_ago)
    recent_revenue = sum(o.total_price for o in recent_orders)
    recent_cogs    = Decimal('0')
    recent_count   = recent_orders.count()
    for o in recent_orders.prefetch_related('items__menu_item__ingredients__ingredient'):
        for oi in o.items.all():
            if oi.menu_item:
                for r in oi.menu_item.ingredients.all():
                    sq = _convert_to_stock_unit(r.quantity_required, r.recipe_unit, r.ingredient.unit)
                    recent_cogs += sq * r.ingredient.cost_per_unit * oi.quantity

    avg_order_value  = (recent_revenue / recent_count).quantize(Decimal('0.01')) if recent_count else Decimal('0')
    avg_gross_margin = float((recent_revenue - recent_cogs) / recent_revenue * 100) if recent_revenue else 0

    # Orders needed per day to break even
    daily_fixed = float(fixed_costs / 30) if fixed_costs else 0
    contrib_per_order = float(avg_order_value) * (avg_gross_margin / 100) if avg_order_value else 0
    breakeven_orders  = int(daily_fixed / contrib_per_order) + 1 if contrib_per_order > 0 else 0

    # Today's order count
    today_orders = Order.objects.filter(
        status='active', created_at__date=timezone.now().date()).count()

    return render(request, 'core/dashboard.html', {
        'ingredients':       ingredients,
        'orders':            orders,
        'total_sales':       total_sales,
        'low_stock':         low_stock,
        'chart_hours':       _json.dumps(chart_hours),
        'chart_counts':      _json.dumps(chart_counts),
        'fixed_costs':       fixed_costs,
        'daily_fixed':       round(daily_fixed, 2),
        'breakeven_orders':  breakeven_orders,
        'today_orders':      today_orders,
        'avg_order_value':   avg_order_value,
        'avg_gross_margin':  round(avg_gross_margin, 1),
        'monthly_payroll':   monthly_payroll,
        'monthly_expenses':  monthly_expenses,
    })

@manager_required
def manage_orders(request):
    """Dedicated order management page — search, filter, edit, void, damage-log."""
    from decimal import Decimal
    from django.core.paginator import Paginator

    _log(request, 'view_dashboard', detail='manage_orders')

    status_filter  = request.GET.get('status', 'active')
    date_from_str  = request.GET.get('date_from', '')
    date_to_str    = request.GET.get('date_to', '')
    search         = request.GET.get('q', '').strip()

    orders = Order.objects.prefetch_related('items__menu_item').order_by('-created_at')

    if status_filter in ('active', 'voided'):
        orders = orders.filter(status=status_filter)

    if date_from_str:
        try:
            orders = orders.filter(created_at__date__gte=datetime.strptime(date_from_str, '%Y-%m-%d').date())
        except ValueError: pass
    if date_to_str:
        try:
            orders = orders.filter(created_at__date__lte=datetime.strptime(date_to_str, '%Y-%m-%d').date())
        except ValueError: pass
    if search:
        orders = orders.filter(order_number__icontains=search)

    paginator = Paginator(orders, 25)
    page_obj  = paginator.get_page(request.GET.get('page'))

    total_shown   = sum(o.total_price for o in page_obj.object_list)
    total_all     = orders.aggregate(Sum('total_price'))['total_price__sum'] or Decimal('0')
    voided_count  = Order.objects.filter(status='voided').count()
    active_count  = Order.objects.filter(status='active').count()

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


@manager_required
def sales_report(request):
    _log(request, 'view_sales_report', detail=f"from={request.GET.get('date_from','')} to={request.GET.get('date_to','')}")
    from decimal import Decimal
    import json as _json

    date_from_str = request.GET.get('date_from', '')
    date_to_str   = request.GET.get('date_to', '')

    orders = Order.objects.filter(status='active').prefetch_related(
        'items__menu_item__ingredients__ingredient'
    ).order_by('-created_at')

    if date_from_str:
        try:
            orders = orders.filter(created_at__date__gte=datetime.strptime(date_from_str, '%Y-%m-%d').date())
        except ValueError: pass
    if date_to_str:
        try:
            orders = orders.filter(created_at__date__lte=datetime.strptime(date_to_str, '%Y-%m-%d').date())
        except ValueError: pass

    total_revenue    = sum((o.total_price for o in orders), Decimal('0'))
    total_discounts  = sum((o.discount_amount for o in orders), Decimal('0'))
    total_orders     = orders.count()
    total_items_sold = sum(oi.quantity for o in orders for oi in o.items.all())

    # COGS + per-item stats
    total_cogs  = Decimal('0')
    item_stats  = {}
    daily_data  = {}

    for order in orders:
        day = order.created_at.strftime('%Y-%m-%d')
        if day not in daily_data:
            daily_data[day] = {'revenue': Decimal('0'), 'cost': Decimal('0')}
        daily_data[day]['revenue'] += order.total_price

        for oi in order.items.all():
            if not oi.menu_item:
                continue
            item_cost = Decimal('0')
            for r in oi.menu_item.ingredients.all():
                sq = _convert_to_stock_unit(r.quantity_required, r.recipe_unit, r.ingredient.unit)
                item_cost += sq * r.ingredient.cost_per_unit * oi.quantity
            total_cogs += item_cost
            daily_data[day]['cost'] += item_cost

            name = oi.menu_item.name
            if name not in item_stats:
                item_stats[name] = {'name': name, 'qty': 0, 'revenue': Decimal('0'), 'cost': Decimal('0')}
            item_stats[name]['qty']     += oi.quantity
            item_stats[name]['revenue'] += oi.price_at_order * oi.quantity
            item_stats[name]['cost']    += item_cost

    gross_profit = total_revenue - total_cogs

    # Payroll
    all_employees   = Employee.objects.filter(is_active=True)
    monthly_payroll = sum((e.salary for e in all_employees), Decimal('0'))
    if date_from_str and date_to_str:
        try:
            days = (datetime.strptime(date_to_str, '%Y-%m-%d').date() -
                    datetime.strptime(date_from_str, '%Y-%m-%d').date()).days + 1
            period_payroll = (monthly_payroll / 30 * days).quantize(Decimal('0.01'))
        except Exception:
            period_payroll = monthly_payroll
    else:
        period_payroll = monthly_payroll

    # Expenses
    expenses_qs = Expense.objects.all()
    if date_from_str:
        try: expenses_qs = expenses_qs.filter(date__gte=datetime.strptime(date_from_str, '%Y-%m-%d').date())
        except ValueError: pass
    if date_to_str:
        try: expenses_qs = expenses_qs.filter(date__lte=datetime.strptime(date_to_str, '%Y-%m-%d').date())
        except ValueError: pass
    total_expenses = sum((e.amount for e in expenses_qs), Decimal('0'))
    expense_by_cat = {}
    for exp in expenses_qs:
        cat = exp.get_category_display()
        expense_by_cat[cat] = expense_by_cat.get(cat, Decimal('0')) + exp.amount

    net_profit   = gross_profit - period_payroll - total_expenses
    gross_margin = float(gross_profit / total_revenue * 100) if total_revenue else 0
    net_margin   = float(net_profit   / total_revenue * 100) if total_revenue else 0

    items_list = sorted(item_stats.values(), key=lambda x: x['qty'], reverse=True)
    for it in items_list:
        it['profit'] = it['revenue'] - it['cost']
        it['margin'] = float(it['profit'] / it['revenue'] * 100) if it['revenue'] else 0

    top_items    = items_list[:5]
    bottom_items = sorted(items_list, key=lambda x: x['margin'])[:3]
    employees    = list(all_employees.order_by('job_title', 'full_name'))

    sorted_days    = sorted(daily_data.keys())[-30:]
    chart_labels   = sorted_days
    chart_revenue  = [float(daily_data[d]['revenue']) for d in sorted_days]
    chart_cost     = [float(daily_data[d]['cost'])    for d in sorted_days]
    chart_profit   = [round(r - c, 2) for r, c in zip(chart_revenue, chart_cost)]

    return render(request, 'core/sales_report.html', {
        'date_from': date_from_str, 'date_to': date_to_str,
        'total_revenue': total_revenue, 'total_discounts': total_discounts,
        'total_orders': total_orders, 'total_items_sold': total_items_sold,
        'total_cogs': total_cogs, 'gross_profit': gross_profit,
        'gross_margin': round(gross_margin, 1), 'period_payroll': period_payroll,
        'total_expenses': total_expenses, 'expense_by_cat': expense_by_cat,
        'net_profit': net_profit, 'net_margin': round(net_margin, 1),
        'top_items': top_items, 'bottom_items': bottom_items, 'all_items': items_list,
        'employees': employees, 'monthly_payroll': monthly_payroll,
        'chart_labels': _json.dumps(chart_labels),
        'chart_revenue': _json.dumps(chart_revenue),
        'chart_cost': _json.dumps(chart_cost),
        'chart_profit': _json.dumps(chart_profit),
        'orders': orders,
    })


@manager_required
def manage_users(request):
    caller_role = request.user.profile.role
    is_super    = (caller_role == 'superadmin')

    if request.method == 'POST':
        # Only superadmin can create accounts
        if not is_super:
            messages.error(request, "Only the Super Admin can create new accounts.")
            return redirect('manage_users')

        username  = request.POST.get('username', '').strip()
        password  = request.POST.get('password', '').strip()
        password2 = request.POST.get('password2', '').strip()
        role      = request.POST.get('role', 'cashier')

        # Validation
        errors = []
        if not username:
            errors.append("Username is required.")
        if not password:
            errors.append("Password is required.")
        elif len(password) < 8:
            errors.append("Password must be at least 8 characters.")
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
    target      = get_object_or_404(User, id=user_id)
    caller      = request.user
    caller_role = caller.profile.role
    is_super    = (caller_role == 'superadmin')
    is_manager  = (caller_role == 'manager')
    target_role = target.profile.role if hasattr(target, 'profile') else 'cashier'
    is_own      = (caller.id == target.id)

    # Permission matrix:
    # superadmin  → can edit anyone
    # manager     → can edit cashiers OR their own account only
    # cashier     → cannot reach this view (manager_required blocks them)
    if not is_super:
        if not is_own and target_role != 'cashier':
            messages.error(request, "You don't have permission to edit this account.")
            return redirect('manage_users')

    if request.method == 'POST':
        new_username  = request.POST.get('username', '').strip()
        new_password  = request.POST.get('new_password', '').strip()
        new_password2 = request.POST.get('new_password2', '').strip()
        old_password  = request.POST.get('old_password', '').strip()
        new_role      = request.POST.get('role', target_role)

        errors = []

        # Username
        if new_username and new_username != target.username:
            if User.objects.filter(username=new_username).exclude(id=user_id).exists():
                errors.append(f"Username '{new_username}' is already taken.")
            else:
                target.username = new_username

        # Role — only superadmin can change roles; managers keep cashier role
        if is_super:
            if new_role in ('superadmin', 'manager', 'cashier'):
                profile, _ = UserProfile.objects.get_or_create(user=target)
                profile.role = new_role
                profile.save()
        # managers cannot change roles at all

        # Password change — requires old password verification
        if new_password or old_password:
            if not old_password:
                errors.append("Current password is required to set a new password.")
            elif not target.check_password(old_password):
                errors.append("Current password is incorrect.")
            elif len(new_password) < 8:
                errors.append("New password must be at least 8 characters.")
            elif new_password != new_password2:
                errors.append("New passwords do not match.")
            else:
                target.set_password(new_password)

        if errors:
            for e in errors:
                messages.error(request, e)
            return redirect('manage_users')

        target.save()
        messages.success(request, f"Account '{target.username}' updated successfully.")
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
    target      = get_object_or_404(User, id=user_id)
    caller_role = request.user.profile.role
    # Only superadmin can delete users
    if caller_role != 'superadmin':
        messages.error(request, "Only the Super Admin can delete accounts.")
        return redirect('manage_users')
    username = target.username
    try:
        target.delete()
        _log(request, 'delete_user', username)
        messages.success(request, f"User '{username}' deleted.")
    except Exception as e:
        messages.error(request, f"Could not delete '{username}': {str(e)}")
    return redirect('manage_users')


@manager_required
def manage_ingredients(request):
    if request.method == 'POST':
        action = request.POST.get('action', 'add')
        from decimal import Decimal

        if action == 'add':
            name = request.POST.get('name', '').strip()
            unit = request.POST.get('unit', 'kg')
            if not name:
                messages.error(request, 'Ingredient name is required.')
            elif Ingredient.objects.filter(name__iexact=name).exists():
                messages.error(request, f"Ingredient '{name}' already exists.")
            else:
                if unit == 'boxes':
                    num_boxes     = Decimal(request.POST.get('num_boxes', '0') or '0')
                    units_per_box = int(request.POST.get('units_per_box', '1') or '1')
                    sub_unit      = request.POST.get('sub_unit', 'packets')
                    total_cost    = Decimal(request.POST.get('total_cost', '0') or '0')
                    total_units   = num_boxes * units_per_box
                    cpu           = (total_cost / total_units).quantize(Decimal('0.0001')) if total_units else Decimal('0')
                    Ingredient.objects.create(
                        name=name, unit='boxes',
                        stock_quantity=total_units,
                        cost_per_unit=cpu,
                        units_per_box=units_per_box,
                        sub_unit=sub_unit,
                    )
                    _log(request, 'add_ingredient', name, detail=f"unit=boxes sub={sub_unit} qty={total_units:.0f} cost/unit={cpu}")
                    messages.success(request, f"'{name}' added: {total_units:.0f} {sub_unit} ({num_boxes} boxes × {units_per_box}/box). Cost/unit: {cpu} EGP.")
                else:
                    # Support "total quantity + total cost" OR "quantity + cost per unit"
                    input_mode   = request.POST.get('add_input_mode', 'per_unit')  # 'total' or 'per_unit'
                    input_unit   = request.POST.get('add_input_unit', unit)        # e.g. 'g' when stock unit is 'kg'

                    raw_qty      = Decimal(request.POST.get('stock_quantity', '0') or '0')
                    # Convert input unit → stock unit if needed
                    if input_unit == 'g' and unit == 'kg':
                        stock_quantity = (raw_qty / 1000).quantize(Decimal('0.001'))
                    elif input_unit == 'mL' and unit == 'L':
                        stock_quantity = (raw_qty / 1000).quantize(Decimal('0.001'))
                    else:
                        stock_quantity = raw_qty

                    if input_mode == 'total':
                        total_cost    = Decimal(request.POST.get('total_cost_plain', '0') or '0')
                        cost_per_unit = (total_cost / stock_quantity).quantize(Decimal('0.0001')) if stock_quantity else Decimal('0')
                    else:
                        cost_per_unit = Decimal(request.POST.get('cost_per_unit', '0') or '0')

                    Ingredient.objects.create(name=name, unit=unit,
                        stock_quantity=stock_quantity, cost_per_unit=cost_per_unit)
                    _log(request, 'add_ingredient', name, detail=f"unit={unit} qty={stock_quantity} cost/unit={cost_per_unit}")
                    messages.success(request, f"Ingredient '{name}' added. Stock: {stock_quantity} {unit}. Cost/unit: {cost_per_unit} EGP.")

        elif action == 'restock':
            ing_id = request.POST.get('ingredient_id')
            try:
                ingredient = get_object_or_404(Ingredient, id=ing_id)
                old_qty  = ingredient.stock_quantity
                old_cost = ingredient.cost_per_unit
                if ingredient.unit == 'boxes':
                    num_boxes     = Decimal(request.POST.get('num_boxes', '0') or '0')
                    units_per_box = int(request.POST.get('units_per_box', str(ingredient.units_per_box)) or ingredient.units_per_box)
                    total_cost    = Decimal(request.POST.get('total_cost', '0') or '0')
                    add_qty_d     = num_boxes * units_per_box
                    new_cost_d    = (total_cost / add_qty_d).quantize(Decimal('0.0001')) if add_qty_d else old_cost
                    ingredient.units_per_box = units_per_box
                else:
                    restock_input_unit = request.POST.get('restock_input_unit', ingredient.unit)
                    restock_input_mode = request.POST.get('restock_input_mode', 'total')  # 'total' or 'per_unit'
                    raw_add = Decimal(request.POST.get('add_quantity', '0') or '0')

                    # Convert input sub-unit to stock unit
                    if restock_input_unit == 'g' and ingredient.unit == 'kg':
                        add_qty_d = (raw_add / 1000).quantize(Decimal('0.001'))
                    elif restock_input_unit == 'mL' and ingredient.unit == 'L':
                        add_qty_d = (raw_add / 1000).quantize(Decimal('0.001'))
                    else:
                        add_qty_d = raw_add

                    if restock_input_mode == 'total':
                        total_cost = Decimal(request.POST.get('total_cost', '0') or '0')
                        new_cost_d = (total_cost / add_qty_d).quantize(Decimal('0.0001')) if add_qty_d else old_cost
                    else:
                        raw_cpu    = request.POST.get('new_cost', '').strip()
                        new_cost_d = Decimal(raw_cpu) if raw_cpu else old_cost
                        total_cost = add_qty_d * new_cost_d

                total_qty = old_qty + add_qty_d
                avg_cost  = (old_qty * old_cost + add_qty_d * new_cost_d) / total_qty if total_qty > 0 else new_cost_d

                old_cost_val = ingredient.cost_per_unit
                ingredient.stock_quantity = total_qty
                ingredient.cost_per_unit  = avg_cost.quantize(Decimal('0.0001'))
                ingredient.save()

                _log_stock(ingredient, add_qty_d, 'restock', request.user)
                CostHistory.objects.create(
                    ingredient=ingredient, old_cost=old_cost_val,
                    new_cost=ingredient.cost_per_unit, qty_restocked=add_qty_d,
                    total_paid=total_cost,
                    recorded_by=request.user if request.user.is_authenticated else None,
                )
                _log(request, 'restock_ingredient', ingredient.name, detail=f"added={add_qty_d} new_avg_cost={ingredient.cost_per_unit}")
                if ingredient.unit == 'boxes':
                    messages.success(request, f"Restocked '{ingredient.name}': added {add_qty_d:.0f} {ingredient.sub_unit}. Stock: {total_qty:.0f}. Cost/{ingredient.sub_unit}: {ingredient.cost_per_unit} EGP.")
                else:
                    messages.success(request, f"Restocked '{ingredient.name}' by {add_qty_d} {ingredient.unit}. Avg cost: {ingredient.cost_per_unit} EGP/{ingredient.unit}.")
            except Exception as e:
                messages.error(request, f"Error restocking: {str(e)}")

        elif action == 'edit':
            ing_id = request.POST.get('ingredient_id')
            try:
                ingredient = get_object_or_404(Ingredient, id=ing_id)
                ingredient.name = request.POST.get('name', ingredient.name).strip() or ingredient.name
                ingredient.unit = request.POST.get('unit', ingredient.unit)

                # For box ingredients: recalculate cost_per_unit from box cost ÷ units_per_box
                if ingredient.unit == 'boxes':
                    new_upb = request.POST.get('edit_units_per_box', '').strip()
                    new_sub = request.POST.get('edit_sub_unit', '').strip()
                    box_cpu = request.POST.get('cost_per_unit', '').strip()  # already per-unit from JS
                    if new_upb:
                        ingredient.units_per_box = max(1, int(new_upb))
                    if new_sub:
                        ingredient.sub_unit = new_sub
                    if box_cpu:
                        box_cpu = box_cpu.replace(',', '.').split()[0]
                        ingredient.cost_per_unit = Decimal(box_cpu)
                else:
                    # cost_per_unit — only update if a value was submitted
                    raw_cpu = request.POST.get('cost_per_unit', '').strip()
                    if raw_cpu:
                        raw_cpu = raw_cpu.replace(',', '.').split()[0]
                        ingredient.cost_per_unit = Decimal(raw_cpu)

                # low_stock_threshold — only update if a value was submitted
                raw_thr = request.POST.get('low_stock_threshold', '').strip()
                if raw_thr:
                    raw_thr = raw_thr.replace(',', '.').split()[0]
                    ingredient.low_stock_threshold = Decimal(raw_thr)

                # stock_quantity — only update if explicitly submitted (not empty)
                raw_stock = request.POST.get('stock_quantity', '').strip()
                if raw_stock:
                    ingredient.stock_quantity = Decimal(raw_stock)

                ingredient.save()
                _log(request, 'edit_ingredient', ingredient.name,
                     detail=f"threshold={ingredient.low_stock_threshold} cost={ingredient.cost_per_unit}")
                messages.success(request, f"Ingredient '{ingredient.name}' updated.")
            except Exception as e:
                messages.error(request, f"Error updating: {str(e)}")

        return redirect('manage_ingredients_page')
    return redirect('manage_ingredients_page')


@manager_required
def manual_stock_adjust(request):
    """
    POST-only: manually add or subtract stock from an ingredient.
    action_type: 'add' | 'subtract' | 'set'
    """
    if request.method != 'POST':
        return redirect('dashboard')
    from decimal import Decimal, InvalidOperation
    ing_id      = request.POST.get('ingredient_id')
    action_type = request.POST.get('action_type', 'add')   # add | subtract | set
    note        = request.POST.get('note', '').strip()

    try:
        ingredient = get_object_or_404(Ingredient, id=ing_id)
        raw_qty    = request.POST.get('quantity', '').strip()
        if not raw_qty:
            messages.error(request, 'Please enter a quantity.')
            return redirect('dashboard')
        qty = Decimal(raw_qty)
        if qty < 0:
            messages.error(request, 'Quantity must be positive.')
            return redirect('dashboard')

        old_qty = ingredient.stock_quantity

        if action_type == 'add':
            change = qty
            ingredient.stock_quantity = old_qty + qty
            verb = f'+{qty}'
        elif action_type == 'subtract':
            change = -qty
            ingredient.stock_quantity = max(Decimal('0'), old_qty - qty)
            verb = f'-{qty}'
        else:  # set
            change = qty - old_qty
            ingredient.stock_quantity = qty
            verb = f'set to {qty}'

        ingredient.save()
        _log_stock(ingredient, change, 'manual', request.user,
                   note=note or f'Manual adjustment: {verb}')
        _log(request, 'edit_ingredient', ingredient.name,
             detail=f'Manual stock adjustment: {verb} {ingredient.unit}. Note: {note}')
        messages.success(request,
            f"Stock for '{ingredient.name}' updated: {verb} {ingredient.unit}. "
            f"New stock: {ingredient.stock_quantity} {ingredient.unit}.")
    except (InvalidOperation, Exception) as e:
        messages.error(request, f'Error adjusting stock: {str(e)}')

    return redirect('dashboard')


def _build_menu_cost(menu_items_raw):
    result = []
    for item in menu_items_raw:
        total_cost = 0.0
        breakdown  = []
        for recipe_item in item.ingredients.all():
            ing       = recipe_item.ingredient
            sq        = float(_convert_to_stock_unit(recipe_item.quantity_required, recipe_item.recipe_unit, ing.unit))
            line_cost = sq * float(ing.cost_per_unit)
            total_cost += line_cost
            breakdown.append({'name': ing.name, 'qty': recipe_item.quantity_required,
                               'unit': ing.unit, 'cost_per_unit': ing.cost_per_unit, 'line_cost': line_cost})
        profit = float(item.price) - total_cost
        margin = (profit / float(item.price) * 100) if float(item.price) > 0 else 0
        result.append({'name': item.name, 'price': item.price, 'is_active': item.is_active,
                       'total_cost': total_cost, 'profit': profit, 'margin': margin,
                       'ingredient_breakdown': breakdown})
    return result


@manager_required
def manage_menu(request):
    menu_items  = MenuItem.objects.prefetch_related('ingredients__ingredient').all()
    ingredients = Ingredient.objects.all()
    categories  = MenuCategory.objects.all()
    if request.method == 'POST':
        name        = request.POST.get('name')
        price       = request.POST.get('price')
        description = request.POST.get('description', '')
        image_url   = request.POST.get('image_url', '')
        is_active   = request.POST.get('is_active') == 'on'
        category_id = request.POST.get('category', '')
        ingredient_ids = request.POST.getlist('ingredients')
        quantities     = request.POST.getlist('quantities')
        recipe_units   = request.POST.getlist('recipe_units')
        if name and price:
            try:
                with transaction.atomic():
                    cat = MenuCategory.objects.get(id=category_id) if category_id else None
                    menu_item = MenuItem.objects.create(name=name, price=price, description=description,
                        image_url=image_url, is_active=is_active, category=cat)
                    for i_id, qty, runit in zip(ingredient_ids, quantities, recipe_units):
                        if i_id and qty and float(qty) > 0:
                            ingredient = get_object_or_404(Ingredient, id=i_id)
                            if not _validate_recipe_unit(ingredient, runit):
                                raise ValueError(f"Unit '{runit}' is not valid for '{ingredient.name}' (stock unit: {ingredient.unit}, sub-unit: {ingredient.sub_unit if ingredient.unit == 'boxes' else '—'})")
                            MenuItemIngredient.objects.create(menu_item=menu_item, ingredient=ingredient,
                                quantity_required=qty, recipe_unit=runit)
                    messages.success(request, f"Menu item '{name}' created.")
                    _log(request, 'add_menu_item', name, detail=f"price={price} category={cat}")
                    return redirect('manage_menu')
            except Exception as e:
                messages.error(request, f"Error creating menu item: {str(e)}")
        else:
            messages.error(request, "Name and price are required.")
    return render(request, 'core/menu_management.html', {
        'menu_items': menu_items, 'ingredients': ingredients, 'categories': categories,
        'menu_items_with_cost': _build_menu_cost(menu_items),
    })


@manager_required
def edit_menu_item(request, pk):
    menu_item   = get_object_or_404(MenuItem, pk=pk)
    ingredients = Ingredient.objects.all()
    categories  = MenuCategory.objects.all()
    if request.method == 'POST':
        menu_item.name        = request.POST.get('name')
        menu_item.price       = request.POST.get('price')
        menu_item.description = request.POST.get('description', '')
        menu_item.image_url   = request.POST.get('image_url', '')
        menu_item.is_active   = request.POST.get('is_active') == 'on'
        category_id           = request.POST.get('category', '')
        menu_item.category    = MenuCategory.objects.get(id=category_id) if category_id else None
        ingredient_ids = request.POST.getlist('ingredients')
        quantities     = request.POST.getlist('quantities')
        recipe_units   = request.POST.getlist('recipe_units')
        if menu_item.name and menu_item.price:
            try:
                with transaction.atomic():
                    menu_item.save()
                    menu_item.ingredients.all().delete()
                    for i_id, qty, runit in zip(ingredient_ids, quantities, recipe_units):
                        if i_id and qty and float(qty) > 0:
                            ingredient = get_object_or_404(Ingredient, id=i_id)
                            if not _validate_recipe_unit(ingredient, runit):
                                raise ValueError(f"Unit '{runit}' is not valid for '{ingredient.name}' (stock unit: {ingredient.unit}, sub-unit: {ingredient.sub_unit if ingredient.unit == 'boxes' else '—'})")
                            MenuItemIngredient.objects.create(menu_item=menu_item, ingredient=ingredient,
                                quantity_required=qty, recipe_unit=runit)
                    messages.success(request, f"Menu item '{menu_item.name}' updated.")
                    _log(request, 'edit_menu_item', menu_item.name, detail=f"price={menu_item.price}")
                    return redirect('manage_menu')
            except Exception as e:
                messages.error(request, f"Error updating menu item: {str(e)}")
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


# ── PDF helpers ────────────────────────────────────────────────────────────────
from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from django.http import HttpResponse

PAGE_W      = 17 * cm
ORANGE      = colors.HexColor('#FF4500')
LIGHT_ORANGE= colors.HexColor('#FFF5F2')
GREY        = colors.HexColor('#6b7280')
LIGHT_GREY  = colors.HexColor('#f3f4f6')
MID_GREY    = colors.HexColor('#e5e7eb')

def _make_doc(buf):
    return SimpleDocTemplate(buf, pagesize=A4,
        leftMargin=2*cm, rightMargin=2*cm, topMargin=2*cm, bottomMargin=2*cm)

def _title_styles():
    title = ParagraphStyle('RPTitle', fontSize=18, textColor=ORANGE, fontName='Helvetica-Bold', spaceAfter=3, leading=22)
    sub   = ParagraphStyle('RPSub',   fontSize=9,  textColor=GREY,   fontName='Helvetica', spaceAfter=0)
    bold8 = ParagraphStyle('RPBold8', fontSize=8,  fontName='Helvetica-Bold')
    norm8 = ParagraphStyle('RPNorm8', fontSize=8,  fontName='Helvetica')
    return title, sub, bold8, norm8

def _base_table_style():
    return TableStyle([
        ('BACKGROUND',    (0,0), (-1,0), ORANGE),
        ('TEXTCOLOR',     (0,0), (-1,0), colors.white),
        ('FONTNAME',      (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE',      (0,0), (-1,0), 8),
        ('FONTNAME',      (0,1), (-1,-1), 'Helvetica'),
        ('FONTSIZE',      (0,1), (-1,-1), 8),
        ('ROWBACKGROUNDS',(0,1), (-1,-1), [colors.white, LIGHT_GREY]),
        ('GRID',          (0,0), (-1,-1), 0.3, MID_GREY),
        ('ALIGN',         (0,0), (-1,-1), 'LEFT'),
        ('VALIGN',        (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING',    (0,0), (-1,-1), 5),
        ('BOTTOMPADDING', (0,0), (-1,-1), 5),
        ('LEFTPADDING',   (0,0), (-1,-1), 6),
        ('RIGHTPADDING',  (0,0), (-1,-1), 6),
    ])

def _get_filtered_orders(request):
    date_from_str = request.GET.get('date_from', '')
    date_to_str   = request.GET.get('date_to', '')
    orders = Order.objects.filter(status='active').prefetch_related(
        'items__menu_item__ingredients__ingredient').order_by('created_at')
    if date_from_str:
        try: orders = orders.filter(created_at__date__gte=datetime.strptime(date_from_str, '%Y-%m-%d').date())
        except ValueError: pass
    if date_to_str:
        try: orders = orders.filter(created_at__date__lte=datetime.strptime(date_to_str, '%Y-%m-%d').date())
        except ValueError: pass
    return orders, date_from_str, date_to_str

def _period_label(date_from_str, date_to_str):
    if date_from_str and date_to_str: return f"{date_from_str}  to  {date_to_str}"
    elif date_from_str: return f"From {date_from_str}"
    elif date_to_str:   return f"Until {date_to_str}"
    return "All Time"


@manager_required
def pdf_report_financial(request):
    orders, date_from_str, date_to_str = _get_filtered_orders(request)
    _log(request, 'export_pdf', 'Financial Report', detail=f"from={date_from_str} to={date_to_str}")
    title_style, sub_style, bold8, norm8 = _title_styles()
    buf = BytesIO()
    doc = _make_doc(buf)
    story = []
    story.append(Paragraph("Searing Sandwiches", title_style))
    story.append(Paragraph("Financial Report", ParagraphStyle('sub2', fontSize=13, textColor=GREY, fontName='Helvetica', spaceAfter=3)))
    story.append(Paragraph(f"Period: {_period_label(date_from_str, date_to_str)}", sub_style))
    story.append(Spacer(1, 0.3*cm))
    story.append(HRFlowable(width="100%", thickness=1.5, color=ORANGE))
    story.append(Spacer(1, 0.4*cm))
    headers = [Paragraph(f'<b>{h}</b>', bold8) for h in
               ['Order #','Date','Time','Product','Unit Cost','Sale Price','Qty','Full Cost','Revenue','Profit']]
    col_w = [2.0*cm, 2.2*cm, 1.6*cm, 3.2*cm, 2.0*cm, 2.0*cm, 1.0*cm, 2.0*cm, 2.0*cm, 2.0*cm]
    rows = [headers]
    grand_cost = grand_revenue = grand_profit = 0.0
    for order in orders:
        order_date = order.created_at.strftime('%Y-%m-%d')
        order_time = order.created_at.strftime('%H:%M')
        for oi in order.items.all():
            if not oi.menu_item: continue
            unit_cost = sum(float(_convert_to_stock_unit(r.quantity_required, r.recipe_unit, r.ingredient.unit)) * float(r.ingredient.cost_per_unit) for r in oi.menu_item.ingredients.all())
            qty = oi.quantity; sale_price = float(oi.price_at_order)
            full_cost = unit_cost * qty; full_revenue = sale_price * qty; profit = full_revenue - full_cost
            grand_cost += full_cost; grand_revenue += full_revenue; grand_profit += profit
            rows.append([Paragraph(order.order_number, norm8), Paragraph(order_date, norm8),
                         Paragraph(order_time, norm8), Paragraph(oi.menu_item.name, norm8),
                         Paragraph(f"{unit_cost:.2f}", norm8), Paragraph(f"{sale_price:.2f}", norm8),
                         Paragraph(str(qty), norm8), Paragraph(f"{full_cost:.2f}", norm8),
                         Paragraph(f"{full_revenue:.2f}", norm8), Paragraph(f"{profit:.2f}", norm8)])
    rows.append([Paragraph('<b>TOTAL</b>', bold8), '', '', '', '', '', '',
                 Paragraph(f'<b>{grand_cost:.2f}</b>', bold8),
                 Paragraph(f'<b>{grand_revenue:.2f}</b>', bold8),
                 Paragraph(f'<b>{grand_profit:.2f}</b>', bold8)])
    ts = _base_table_style()
    ts.add('BACKGROUND', (0,-1), (-1,-1), LIGHT_ORANGE)
    ts.add('LINEABOVE',  (0,-1), (-1,-1), 1.2, ORANGE)
    ts.add('SPAN',       (0,-1), (6,-1))
    t = Table(rows, colWidths=col_w, repeatRows=1)
    t.setStyle(ts)
    story.append(t)
    story.append(Spacer(1, 0.6*cm))
    monthly_payroll = float(sum(e.salary for e in Employee.objects.filter(is_active=True)))
    net_profit_val  = grand_profit - monthly_payroll
    summary = [
        [Paragraph('Total Ingredient Cost', norm8), Paragraph(f'{grand_cost:.2f} EGP', norm8)],
        [Paragraph('Total Revenue',         norm8), Paragraph(f'{grand_revenue:.2f} EGP', norm8)],
        [Paragraph('Gross Profit',          norm8), Paragraph(f'{grand_profit:.2f} EGP', norm8)],
        [Paragraph('Monthly Payroll',       norm8), Paragraph(f'- {monthly_payroll:.2f} EGP',
            ParagraphStyle('red', fontName='Helvetica', fontSize=8, textColor=colors.HexColor('#dc2626')))],
        [Paragraph('<b>Net Profit</b>', bold8), Paragraph(f'<b>{net_profit_val:.2f} EGP</b>',
            ParagraphStyle('profit', fontName='Helvetica-Bold', fontSize=8,
                textColor=ORANGE if net_profit_val >= 0 else colors.HexColor('#dc2626')))],
    ]
    st = Table(summary, colWidths=[4.5*cm, 4*cm])
    st.setStyle(TableStyle([('FONTSIZE',(0,0),(-1,-1),8),('ALIGN',(1,0),(1,-1),'RIGHT'),
        ('TOPPADDING',(0,0),(-1,-1),4),('BOTTOMPADDING',(0,0),(-1,-1),4),
        ('LINEBELOW',(0,2),(-1,2),0.5,MID_GREY),('LINEABOVE',(0,4),(-1,4),1,ORANGE),
        ('BACKGROUND',(0,4),(-1,4),LIGHT_ORANGE)]))
    story.append(st)
    doc.build(story)
    buf.seek(0)
    resp = HttpResponse(buf, content_type='application/pdf')
    resp['Content-Disposition'] = 'attachment; filename="financial_report.pdf"'
    return resp


@manager_required
def pdf_report_ingredients(request):
    orders, date_from_str, date_to_str = _get_filtered_orders(request)
    _log(request, 'export_pdf', 'Ingredient Usage Report', detail=f"from={date_from_str} to={date_to_str}")
    title_style, sub_style, bold8, norm8 = _title_styles()
    item_qty = {}; item_names = {}
    for order in orders:
        for oi in order.items.all():
            if not oi.menu_item: continue
            mid = oi.menu_item_id
            item_qty[mid]   = item_qty.get(mid, 0) + oi.quantity
            item_names[mid] = oi.menu_item
    buf = BytesIO(); doc = _make_doc(buf); story = []
    story.append(Paragraph("Searing Sandwiches", title_style))
    story.append(Paragraph("Ingredient Usage Report", ParagraphStyle('sub2', fontSize=13, textColor=GREY, fontName='Helvetica', spaceAfter=3)))
    story.append(Paragraph(f"Period: {_period_label(date_from_str, date_to_str)}", sub_style))
    story.append(Spacer(1, 0.3*cm))
    story.append(HRFlowable(width="100%", thickness=1.5, color=ORANGE))
    story.append(Spacer(1, 0.4*cm))
    headers = [Paragraph(f'<b>{h}</b>', bold8) for h in ['Product Name','Qty Sold','Ingredient','Qty Used','Unit','Ing. Cost (EGP)']]
    col_w = [4.5*cm, 2.0*cm, 4.5*cm, 2.5*cm, 1.5*cm, 2.0*cm]
    rows = [headers]
    for mid, menu_item in item_names.items():
        qty_sold = item_qty[mid]
        recipes  = list(menu_item.ingredients.all())
        if not recipes:
            rows.append([Paragraph(menu_item.name, norm8), Paragraph(str(qty_sold), norm8),
                         Paragraph('—', norm8), Paragraph('—', norm8), Paragraph('—', norm8), Paragraph('—', norm8)])
            continue
        for i, recipe in enumerate(recipes):
            ing = recipe.ingredient
            sq  = float(_convert_to_stock_unit(recipe.quantity_required, recipe.recipe_unit, ing.unit))
            total_qty_used = sq * qty_sold
            ing_cost       = total_qty_used * float(ing.cost_per_unit)
            rows.append([Paragraph(menu_item.name if i == 0 else '', norm8),
                         Paragraph(str(qty_sold) if i == 0 else '', norm8),
                         Paragraph(ing.name, norm8), Paragraph(f"{total_qty_used:.3f}", norm8),
                         Paragraph(ing.unit, norm8), Paragraph(f"{ing_cost:.2f}", norm8)])
        rows.append(['', '', '', '', '', ''])
    ts = _base_table_style()
    for i, row in enumerate(rows):
        if i > 0 and all(str(c) == '' for c in row):
            ts.add('LINEABOVE', (0,i), (-1,i), 0.6, MID_GREY)
            ts.add('TOPPADDING', (0,i), (-1,i), 2)
            ts.add('BOTTOMPADDING', (0,i), (-1,i), 2)
    t = Table(rows, colWidths=col_w, repeatRows=1)
    t.setStyle(ts)
    story.append(t)
    doc.build(story)
    buf.seek(0)
    resp = HttpResponse(buf, content_type='application/pdf')
    resp['Content-Disposition'] = 'attachment; filename="ingredient_usage_report.pdf"'
    return resp


@manager_required
def delete_ingredient(request, ingredient_id):
    ingredient = get_object_or_404(Ingredient, id=ingredient_id)
    name = ingredient.name

    # Block if still used in active menu item recipes
    recipe_count = MenuItemIngredient.objects.filter(ingredient=ingredient).count()
    if recipe_count > 0:
        messages.error(request,
            f"Cannot delete '{name}' — it is used in {recipe_count} menu item recipe(s). "
            f"Remove it from all recipes first.")
        return redirect('manage_ingredients_page')

    try:
        ingredient.delete()
        _log(request, 'delete_ingredient', name)
        messages.success(request, f"Ingredient '{name}' deleted successfully.")
    except Exception as e:
        messages.error(request, f"Could not delete '{name}': {str(e)}")
    return redirect('manage_ingredients_page')


@manager_required
def archive_ingredient(request, ingredient_id):
    """Archive an ingredient (soft-delete) — preserves PO and stock history."""
    ingredient = get_object_or_404(Ingredient, id=ingredient_id)
    ingredient.is_active = not ingredient.is_active
    ingredient.save()
    action = 'activated' if ingredient.is_active else 'archived'
    _log(request, 'edit_ingredient', ingredient.name, detail=f'{action} (is_active={ingredient.is_active})')
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
    except Exception as e:
        messages.error(request, f"Cannot delete '{name}': {str(e)}")
    return redirect('manage_menu')


@manager_required
def delete_order(request, order_id):
    if request.method != 'POST':
        return redirect('dashboard')
    order = get_object_or_404(Order, id=order_id)
    order_number = order.order_number
    try:
        order.delete()
        _log(request, 'delete_order', order_number)
        messages.success(request, f"Order #{order_number} deleted.")
    except Exception as e:
        messages.error(request, f"Could not delete order #{order_number}: {str(e)}")
    return redirect('dashboard')


# ── Edit Order (manager/superadmin only) ───────────────────────────────────────

@manager_required
def edit_order(request, order_id):
    """
    Allow managers to fix errors in an order:
    - Change quantity of any item
    - Remove an item entirely
    - Update table number, customer notes
    Original created_at timestamp is NEVER changed.
    All changes are logged in ActivityLog + StockLog.
    """
    from decimal import Decimal
    order = get_object_or_404(Order, id=order_id)

    if order.status == 'voided':
        messages.error(request, "Cannot edit a voided order.")
        return redirect('dashboard')

    if request.method == 'POST':
        with transaction.atomic():
            changes = []

            # --- Table / notes ---
            new_table = request.POST.get('table_number', '').strip()
            new_notes = request.POST.get('customer_notes', '').strip()
            if new_table != order.table_number:
                changes.append(f"table: '{order.table_number}'→'{new_table}'")
                order.table_number = new_table
            if new_notes != order.customer_notes:
                changes.append("notes updated")
                order.customer_notes = new_notes

            # --- Line items ---
            new_total = Decimal('0')
            for oi in list(order.items.all()):
                key = f'qty_{oi.id}'
                if key not in request.POST:
                    continue
                new_qty_raw = request.POST.get(key, '').strip()

                # Remove item
                if new_qty_raw == '0' or new_qty_raw == '':
                    item_name = oi.menu_item.name if oi.menu_item else '[deleted]'
                    old_qty   = oi.quantity
                    # Restore stock for removed item
                    if oi.menu_item:
                        for recipe in oi.menu_item.ingredients.all():
                            restore = _convert_to_stock_unit(
                                recipe.quantity_required * old_qty,
                                recipe.recipe_unit, recipe.ingredient.unit)
                            ing = Ingredient.objects.select_for_update().get(id=recipe.ingredient.id)
                            before = ing.stock_quantity
                            ing.stock_quantity += restore
                            ing.save()
                            _log_stock(ing, restore, 'edit', request.user,
                                       note=f'Order edit — removed {item_name} from {order.order_number}')
                    changes.append(f"removed '{item_name}' (was ×{old_qty})")
                    oi.delete()
                    continue

                try:
                    new_qty = int(new_qty_raw)
                except ValueError:
                    continue

                if new_qty == oi.quantity:
                    new_total += oi.price_at_order * oi.quantity
                    continue

                item_name = oi.menu_item.name if oi.menu_item else '[deleted]'
                qty_diff  = new_qty - oi.quantity   # positive = added more, negative = reduced

                # Adjust stock
                if oi.menu_item and qty_diff != 0:
                    for recipe in oi.menu_item.ingredients.all():
                        stock_diff = _convert_to_stock_unit(
                            recipe.quantity_required * abs(qty_diff),
                            recipe.recipe_unit, recipe.ingredient.unit)
                        ing = Ingredient.objects.select_for_update().get(id=recipe.ingredient.id)
                        before = ing.stock_quantity
                        if qty_diff > 0:
                            # More items ordered — deduct more stock
                            ing.stock_quantity -= stock_diff
                        else:
                            # Fewer items — restore stock
                            ing.stock_quantity += stock_diff
                        ing.save()
                        _log_stock(ing, stock_diff if qty_diff < 0 else -stock_diff, 'edit',
                                   request.user,
                                   note=f'Order edit — {item_name} ×{oi.quantity}→×{new_qty} ({order.order_number})')

                changes.append(f"'{item_name}' qty ×{oi.quantity}→×{new_qty}")
                oi.quantity = new_qty
                # Re-snapshot COGS for new qty
                if oi.menu_item:
                    try:
                        new_cogs = Decimal('0')
                        for recipe in oi.menu_item.ingredients.all():
                            sq = _convert_to_stock_unit(recipe.quantity_required * new_qty,
                                                        recipe.recipe_unit, recipe.ingredient.unit)
                            new_cogs += sq * recipe.ingredient.cost_per_unit
                        oi.cogs_at_sale = new_cogs
                    except Exception:
                        pass
                oi.save()
                new_total += oi.price_at_order * new_qty

            # Recalculate total (keep discount)
            order.total_price = max(Decimal('0'), new_total - order.discount_amount)
            order.save()

            if changes:
                detail_str = '; '.join(changes)
                _log(request, 'edit_order', order.order_number, detail=detail_str)
                messages.success(request, f"Order #{order.order_number} updated. Changes: {detail_str}")
            else:
                messages.info(request, "No changes were made.")

        return redirect('dashboard')

    # GET — render edit form
    items = order.items.select_related('menu_item').all()
    return render(request, 'core/edit_order.html', {'order': order, 'items': items})


# ── Log Damaged Stock (from order) ────────────────────────────────────────────

@manager_required
def log_damage(request, order_id):
    """
    Log damaged ingredients from an order.
    User selects which menu items (and how many portions) were damaged;
    the app calculates exact ingredient quantities from the recipe automatically.
    """
    from decimal import Decimal
    order = get_object_or_404(Order, id=order_id)

    if request.method == 'POST':
        with transaction.atomic():
            any_logged = False
            reason_global = request.POST.get('damage_reason_global', '').strip() or 'Damaged during cooking'

            # {ing_id: {'qty': Decimal, 'unit': str}}
            ing_damage = {}

            for oi_id in request.POST.getlist('damaged_oi_id'):
                portions_raw = request.POST.get(f'portions_{oi_id}', '1')
                ticked_raw   = request.POST.get(f'ticked_{oi_id}', '')
                try:
                    portions = Decimal(portions_raw or '1')
                except Exception:
                    portions = Decimal('1')
                if portions <= 0:
                    continue

                # Parse which ingredient indices were ticked
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

                # Only process the ticked ingredient indices
                recipe_list = list(oi.menu_item.ingredients.select_related('ingredient').all())
                for idx, recipe in enumerate(recipe_list):
                    if idx not in ticked_indices:
                        continue  # skip un-ticked ingredients
                    ing = recipe.ingredient
                    qty_in_stock_unit = _convert_to_stock_unit(
                        recipe.quantity_required * portions,
                        recipe.recipe_unit, ing.unit)
                    if ing.id not in ing_damage:
                        ing_damage[ing.id] = {'qty': Decimal('0'), 'unit': ing.unit}
                    ing_damage[ing.id]['qty'] += qty_in_stock_unit

            # Records for journal entry: [{ingredient, qty_damaged, unit_cost}]
            journal_records = []

            for ing_id, data in ing_damage.items():
                qty = data['qty']
                ing = Ingredient.objects.select_for_update().get(id=ing_id)
                unit_cost_snapshot = ing.cost_per_unit  # snapshot before any change

                ing.stock_quantity = max(Decimal('0'), ing.stock_quantity - qty)
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
                # Post double-entry journal: DR Damaged Goods Expense / CR Inventory
                try:
                    from accounting.utils import post_damage_journal
                    post_damage_journal(journal_records, order, request.user)
                except Exception:
                    pass  # never let accounting failure block the damage log

                _log(request, 'log_damage', order.order_number,
                     detail=f"Damage logged from order {order.order_number}")

                total_loss = sum(
                    r['qty_damaged'] * r['unit_cost'] for r in journal_records
                )
                messages.success(
                    request,
                    f"Damage logged for order #{order.order_number}. "
                    f"Stock deducted and expense of {float(total_loss):.2f} EGP "
                    f"posted to journal (Damaged Goods / Wastage)."
                )
            else:
                messages.warning(request, "No ingredients selected for damage.")

        return redirect('dashboard')

    # GET — build per-item recipe breakdown for display
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


# ── Damaged Stock Log View ─────────────────────────────────────────────────────

@manager_required
def damaged_stock_view(request):
    from django.core.paginator import Paginator
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
    page_obj  = paginator.get_page(request.GET.get('page'))
    # Aggregate total damage cost using DB — avoids loading all records in Python
    from django.db.models import F, ExpressionWrapper, DecimalField
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
        'logs': page_obj, 'page_obj': page_obj,
        'ingredients': Ingredient.objects.all().order_by('name'),
        'ing_filter': ing_filter,
        'total_damage_cost': total_damage_cost,
    })


# ── Employee Management ────────────────────────────────────────────────────────

@manager_required
def manage_employees(request):
    employees    = Employee.objects.all().order_by('full_name')
    total_salary = sum(e.salary for e in employees if e.is_active)
    return render(request, 'core/manage_employees.html',
                  {'employees': employees, 'total_salary': total_salary, 'job_choices': Employee.JOB_CHOICES})

@manager_required
def add_employee(request):
    if request.method == 'POST':
        from decimal import Decimal
        full_name = request.POST.get('full_name', '').strip()
        phone     = request.POST.get('phone', '').strip()
        job_title = request.POST.get('job_title', 'cashier')
        salary    = request.POST.get('salary', '0')
        hire_date = request.POST.get('hire_date', '')
        notes     = request.POST.get('notes', '').strip()
        if full_name:
            Employee.objects.create(full_name=full_name, phone=phone, job_title=job_title,
                salary=Decimal(salary or '0'), hire_date=hire_date or timezone.now().date(), notes=notes)
            _log(request, 'add_employee', full_name, detail=f"role={job_title}")
            messages.success(request, f"Employee '{full_name}' added.")
        else:
            messages.error(request, "Full name is required.")
    return redirect('manage_employees')

@manager_required
def edit_employee(request, employee_id):
    emp = get_object_or_404(Employee, id=employee_id)
    if request.method == 'POST':
        from decimal import Decimal
        emp.full_name = request.POST.get('full_name', emp.full_name).strip()
        emp.phone     = request.POST.get('phone', emp.phone).strip()
        emp.job_title = request.POST.get('job_title', emp.job_title)
        emp.salary    = Decimal(request.POST.get('salary', str(emp.salary)) or '0')
        hire_date     = request.POST.get('hire_date', '')
        if hire_date: emp.hire_date = hire_date
        emp.notes     = request.POST.get('notes', emp.notes).strip()
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


# ── Promo Codes ────────────────────────────────────────────────────────────────

@manager_required
def manage_promos(request):
    promos = PromoCode.objects.all().order_by('-created_at')
    return render(request, 'core/manage_promos.html', {'promos': promos})

@manager_required
def add_promo(request):
    if request.method == 'POST':
        from decimal import Decimal
        code  = request.POST.get('code', '').strip().upper()
        desc  = request.POST.get('description', '').strip()
        dtype = request.POST.get('discount_type', 'percent')
        dval  = request.POST.get('discount_value', '0')
        if not code:
            messages.error(request, "Code is required.")
        elif PromoCode.objects.filter(code=code).exists():
            messages.error(request, f"Code '{code}' already exists.")
        else:
            PromoCode.objects.create(code=code, description=desc, discount_type=dtype, discount_value=Decimal(dval or '0'))
            _log(request, 'add_promo', code, detail=f"type={dtype} value={dval}")
            messages.success(request, f"Promo code '{code}' created.")
    return redirect('manage_promos')

@manager_required
def edit_promo(request, promo_id):
    promo = get_object_or_404(PromoCode, id=promo_id)
    if request.method == 'POST':
        from decimal import Decimal
        promo.code           = request.POST.get('code', promo.code).strip().upper()
        promo.description    = request.POST.get('description', promo.description).strip()
        promo.discount_type  = request.POST.get('discount_type', promo.discount_type)
        promo.discount_value = Decimal(request.POST.get('discount_value', str(promo.discount_value)) or '0')
        promo.is_active      = request.POST.get('is_active') == 'on'
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


# ── Expenses ───────────────────────────────────────────────────────────────────

@manager_required
def manage_expenses(request):
    expenses = Expense.objects.all().order_by('-date')
    total    = sum(e.amount for e in expenses)
    return render(request, 'core/manage_expenses.html',
                  {'expenses': expenses, 'total': total, 'category_choices': Expense.CATEGORY_CHOICES})

@manager_required
def add_expense(request):
    if request.method == 'POST':
        from decimal import Decimal
        title    = request.POST.get('title', '').strip()
        category = request.POST.get('category', 'other')
        amount   = request.POST.get('amount', '0')
        date_str = request.POST.get('date', '')
        notes    = request.POST.get('notes', '').strip()
        if title:
            exp = Expense.objects.create(title=title, category=category,
                amount=Decimal(amount or '0'), date=date_str or timezone.now().date(), notes=notes)
            try:
                from accounting.utils import post_expense_journal
                post_expense_journal(exp, request.user)
            except Exception:
                pass
            _log(request, 'add_expense', title, detail=f"amount={amount} cat={category}")
            messages.success(request, f"Expense '{title}' recorded.")
        else:
            messages.error(request, "Title is required.")
    return redirect('manage_expenses')

@manager_required
def edit_expense(request, expense_id):
    exp = get_object_or_404(Expense, id=expense_id)
    if request.method == 'POST':
        from decimal import Decimal
        exp.title    = request.POST.get('title', exp.title).strip()
        exp.category = request.POST.get('category', exp.category)
        exp.amount   = Decimal(request.POST.get('amount', str(exp.amount)) or '0')
        date_str     = request.POST.get('date', '')
        if date_str: exp.date = date_str
        exp.notes    = request.POST.get('notes', exp.notes).strip()
        exp.save()
        _log(request, 'edit_expense', exp.title, detail=f"amount={exp.amount}")
        messages.success(request, f"Expense '{exp.title}' updated.")
    return redirect('manage_expenses')

@manager_required
def delete_expense(request, expense_id):
    exp = get_object_or_404(Expense, id=expense_id)
    _log(request, 'delete_expense', exp.title, detail=f"amount={exp.amount}")
    exp.delete()
    messages.success(request, "Expense deleted.")
    return redirect('manage_expenses')


# ── Shift Report ───────────────────────────────────────────────────────────────

@manager_required
def shift_report(request):
    from decimal import Decimal
    _log(request, 'view_shift_report', detail=f"date={request.GET.get('date','today')}")
    date_str = request.GET.get('date', timezone.now().date().isoformat())
    try:
        report_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        report_date = timezone.now().date()

    orders = Order.objects.filter(status='active').prefetch_related('items__menu_item').filter(
        created_at__date=report_date)

    total_revenue  = sum(o.total_price for o in orders)
    total_discount = sum(o.discount_amount for o in orders)
    total_orders   = orders.count()

    item_summary = {}
    for order in orders:
        for oi in order.items.all():
            name = oi.menu_item.name if oi.menu_item else '[deleted]'
            if name not in item_summary:
                item_summary[name] = {'qty': 0, 'revenue': Decimal('0')}
            item_summary[name]['qty']     += oi.quantity
            item_summary[name]['revenue'] += oi.price_at_order * oi.quantity

    day_expenses   = Expense.objects.filter(date=report_date)
    total_expenses = sum(e.amount for e in day_expenses)

    monthly_payroll = sum((e.salary for e in Employee.objects.filter(is_active=True)), Decimal('0'))
    daily_payroll   = (monthly_payroll / 30).quantize(Decimal('0.01'))

    ingredient_cost = Decimal('0')
    for order in orders:
        for oi in order.items.all():
            if oi.menu_item:
                for recipe in oi.menu_item.ingredients.all():
                    sq = _convert_to_stock_unit(recipe.quantity_required, recipe.recipe_unit, recipe.ingredient.unit)
                    ingredient_cost += sq * recipe.ingredient.cost_per_unit * oi.quantity

    net_profit = total_revenue - ingredient_cost - daily_payroll - total_expenses

    return render(request, 'core/shift_report.html', {
        'report_date': report_date, 'orders': orders,
        'total_revenue': total_revenue, 'total_discount': total_discount, 'total_orders': total_orders,
        'item_summary': item_summary, 'day_expenses': day_expenses, 'total_expenses': total_expenses,
        'daily_payroll': daily_payroll, 'ingredient_cost': ingredient_cost, 'net_profit': net_profit,
    })


@manager_required
def shift_report_pdf(request):
    """Download the daily shift report as a PDF."""
    from decimal import Decimal
    date_str = request.GET.get('date', timezone.now().date().isoformat())
    try:
        report_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        report_date = timezone.now().date()

    orders = Order.objects.filter(status='active').prefetch_related(
        'items__menu_item__ingredients__ingredient').filter(created_at__date=report_date)

    total_revenue  = sum(o.total_price for o in orders)
    total_discount = sum(o.discount_amount for o in orders)
    total_orders   = orders.count()

    item_summary = {}
    for order in orders:
        for oi in order.items.all():
            name = oi.menu_item.name if oi.menu_item else '[deleted]'
            if name not in item_summary:
                item_summary[name] = {'qty': 0, 'revenue': Decimal('0')}
            item_summary[name]['qty']     += oi.quantity
            item_summary[name]['revenue'] += oi.price_at_order * oi.quantity

    day_expenses   = Expense.objects.filter(date=report_date)
    total_expenses = sum(e.amount for e in day_expenses)
    monthly_payroll = sum((e.salary for e in Employee.objects.filter(is_active=True)), Decimal('0'))
    daily_payroll   = (monthly_payroll / 30).quantize(Decimal('0.01'))
    ingredient_cost = Decimal('0')
    for order in orders:
        for oi in order.items.all():
            if oi.menu_item:
                _cogs = getattr(oi, 'cogs_at_sale', None)
                if _cogs and _cogs > 0:
                    ingredient_cost += _cogs
                else:
                    for recipe in oi.menu_item.ingredients.all():
                        sq = _convert_to_stock_unit(recipe.quantity_required, recipe.recipe_unit, recipe.ingredient.unit)
                        ingredient_cost += sq * recipe.ingredient.cost_per_unit * oi.quantity
    net_profit = total_revenue - ingredient_cost - daily_payroll - total_expenses

    from io import BytesIO
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable
    ORANGE = colors.HexColor('#FF4500')
    LGREY  = colors.HexColor('#f3f4f6')
    MGREY  = colors.HexColor('#e5e7eb')
    GREY   = colors.HexColor('#6b7280')
    b8 = ParagraphStyle('b8', fontName='Helvetica-Bold', fontSize=8)
    n8 = ParagraphStyle('n8', fontName='Helvetica',      fontSize=8)
    title_st = ParagraphStyle('T', fontName='Helvetica-Bold', fontSize=16, textColor=ORANGE)
    sub_st   = ParagraphStyle('S', fontName='Helvetica',      fontSize=9,  textColor=GREY)

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
        leftMargin=2*cm, rightMargin=2*cm, topMargin=1.5*cm, bottomMargin=1.5*cm)
    story = []

    story.append(Paragraph('Searing Sandwiches', title_st))
    story.append(Paragraph(f'Daily Shift Report — {report_date.strftime("%A, %d %B %Y")}', sub_st))
    story.append(Spacer(1, .2*cm))
    story.append(HRFlowable(width='100%', thickness=1.5, color=ORANGE))
    story.append(Spacer(1, .4*cm))

    def _ts():
        return TableStyle([
            ('BACKGROUND',    (0,0),(-1,0), ORANGE),
            ('TEXTCOLOR',     (0,0),(-1,0), colors.white),
            ('FONTNAME',      (0,0),(-1,0), 'Helvetica-Bold'),
            ('FONTNAME',      (0,1),(-1,-1),'Helvetica'),
            ('FONTSIZE',      (0,0),(-1,-1), 8),
            ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,LGREY]),
            ('GRID',          (0,0),(-1,-1), 0.3, MGREY),
            ('TOPPADDING',    (0,0),(-1,-1), 4),
            ('BOTTOMPADDING', (0,0),(-1,-1), 4),
            ('LEFTPADDING',   (0,0),(-1,-1), 5),
            ('RIGHTPADDING',  (0,0),(-1,-1), 5),
        ])

    # KPI summary
    np_color = ParagraphStyle('np', fontName='Helvetica-Bold', fontSize=8,
        textColor=ORANGE if net_profit >= 0 else colors.HexColor('#dc2626'))
    red8 = ParagraphStyle('r8', fontName='Helvetica', fontSize=8,
        textColor=colors.HexColor('#dc2626'))
    kpi_rows = [
        [Paragraph('<b>Metric</b>', b8), Paragraph('<b>Amount (EGP)</b>', b8)],
        [Paragraph('Total Orders',  n8), Paragraph(str(total_orders), n8)],
        [Paragraph('Revenue',       n8), Paragraph(f'{float(total_revenue):.2f}', n8)],
        [Paragraph('Discounts',     n8), Paragraph(f'- {float(total_discount):.2f}', red8)],
        [Paragraph('Ingredient Cost',n8),Paragraph(f'- {float(ingredient_cost):.2f}', red8)],
        [Paragraph('Daily Payroll', n8), Paragraph(f'- {float(daily_payroll):.2f}', red8)],
        [Paragraph('Day Expenses',  n8), Paragraph(f'- {float(total_expenses):.2f}', red8)],
        [Paragraph('<b>Net Profit</b>', b8), Paragraph(f'<b>{float(net_profit):.2f}</b>', np_color)],
    ]
    kt = Table(kpi_rows, colWidths=[8*cm, 4*cm])
    kts = _ts()
    kts.add('BACKGROUND', (0,-1),(-1,-1), colors.HexColor('#FFF5F2'))
    kts.add('LINEABOVE',  (0,-1),(-1,-1), 1.2, ORANGE)
    kt.setStyle(kts)
    story.append(kt)
    story.append(Spacer(1, .5*cm))

    # Item breakdown
    story.append(Paragraph('<b>Sales by Item</b>', b8))
    story.append(Spacer(1, .15*cm))
    irows = [[Paragraph('<b>Item</b>',b8), Paragraph('<b>Qty</b>',b8), Paragraph('<b>Revenue (EGP)</b>',b8)]]
    for name, vals in sorted(item_summary.items(), key=lambda x: -x[1]['revenue']):
        irows.append([Paragraph(name,n8), Paragraph(str(vals['qty']),n8),
                      Paragraph(f'{float(vals["revenue"]):.2f}',n8)])
    it = Table(irows, colWidths=[10*cm,2*cm,4*cm], repeatRows=1)
    it.setStyle(_ts())
    story.append(it)

    if day_expenses:
        story.append(Spacer(1, .5*cm))
        story.append(Paragraph('<b>Day Expenses</b>', b8))
        story.append(Spacer(1, .15*cm))
        erows = [[Paragraph('<b>Title</b>',b8),Paragraph('<b>Category</b>',b8),Paragraph('<b>Amount</b>',b8)]]
        for exp in day_expenses:
            erows.append([Paragraph(exp.title,n8),Paragraph(exp.get_category_display(),n8),
                          Paragraph(f'{float(exp.amount):.2f}',n8)])
        et = Table(erows, colWidths=[8*cm,4*cm,4*cm], repeatRows=1)
        et.setStyle(_ts())
        story.append(et)

    doc.build(story)
    buf.seek(0)
    from django.http import HttpResponse
    resp = HttpResponse(buf.read(), content_type='application/pdf')
    resp['Content-Disposition'] = f'attachment; filename="shift_report_{report_date}.pdf"'
    _log(request, 'export_pdf', f'Shift Report {report_date}')
    return resp


# ── Void Order ─────────────────────────────────────────────────────────────────

@manager_required
def void_order(request, order_id):
    from decimal import Decimal
    order = get_object_or_404(Order, id=order_id)
    if order.status == 'voided':
        messages.error(request, f"Order #{order.order_number} is already voided.")
        return redirect(request.META.get('HTTP_REFERER') or reverse('dashboard'))
    if request.method == 'POST':
        reason = request.POST.get('void_reason', '').strip()
        if not reason:
            messages.error(request, 'A reason is required to void an order.')
            return redirect(request.META.get('HTTP_REFERER') or reverse('dashboard'))
        with transaction.atomic():
            for oi in order.items.all():
                if not oi.menu_item: continue
                for recipe in oi.menu_item.ingredients.all():
                    restored = _convert_to_stock_unit(
                        recipe.quantity_required * oi.quantity,
                        recipe.recipe_unit, recipe.ingredient.unit)
                    ing = Ingredient.objects.select_for_update().get(id=recipe.ingredient.id)
                    ing.stock_quantity += restored
                    ing.save()
                    _log_stock(ing, restored, 'void', request.user,
                               note=f'Void order {order.order_number}')
            order.status      = 'voided'
            order.voided_at   = timezone.now()
            order.voided_by   = request.user
            order.void_reason = reason
            order.save()
            try:
                from accounting.utils import post_void_journal
                post_void_journal(order, request.user)
            except Exception:
                pass
        messages.success(request, f"Order #{order.order_number} voided. Stock restored.")
        _log(request, 'void_order', order.order_number, detail=f"reason={reason}")
        return redirect(request.META.get('HTTP_REFERER') or reverse('dashboard'))
    return redirect('dashboard')


# ── Menu Categories ────────────────────────────────────────────────────────────

@manager_required
def manage_categories(request):
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'add':
            name = request.POST.get('name', '').strip()
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
            cat.name       = request.POST.get('name', cat.name).strip()
            cat.sort_order = request.POST.get('sort_order', cat.sort_order)
            cat.save()
            _log(request, 'edit_category', cat.name)
            messages.success(request, f"Category '{cat.name}' updated.")
        elif action == 'delete':
            cat = get_object_or_404(MenuCategory, id=request.POST.get('cat_id'))
            name = cat.name; cat.delete()
            _log(request, 'delete_category', name)
            messages.success(request, f"Category '{name}' deleted.")
        return redirect('manage_categories')
    categories = MenuCategory.objects.annotate(item_count=Count('items'))
    return render(request, 'core/manage_categories.html', {'categories': categories})


# ── Stock Log ──────────────────────────────────────────────────────────────────

@manager_required
def stock_log_view(request):
    from django.core.paginator import Paginator
    _log(request, 'view_stock_log')
    logs = StockLog.objects.select_related('ingredient', 'changed_by').order_by('-created_at')
    ing_filter    = request.GET.get('ingredient', '')
    reason_filter = request.GET.get('reason', '')
    if ing_filter:    logs = logs.filter(ingredient_id=ing_filter)
    if reason_filter: logs = logs.filter(reason=reason_filter)
    paginator = Paginator(logs, 50)
    page_obj  = paginator.get_page(request.GET.get('page'))
    return render(request, 'core/stock_log.html', {
        'logs': page_obj,
        'page_obj': page_obj,
        'ingredients': Ingredient.objects.all().order_by('name'),
        'ing_filter': ing_filter, 'reason_filter': reason_filter,
    })


# ── Cost History ───────────────────────────────────────────────────────────────

@manager_required
def cost_history_view(request):
    from django.core.paginator import Paginator
    _log(request, 'view_cost_history')
    ing_id  = request.GET.get('ingredient', '')
    history = CostHistory.objects.select_related('ingredient', 'recorded_by').order_by('-created_at')
    if ing_id: history = history.filter(ingredient_id=ing_id)
    paginator = Paginator(history, 50)
    page_obj  = paginator.get_page(request.GET.get('page'))
    return render(request, 'core/cost_history.html', {
        'history': page_obj,
        'page_obj': page_obj,
        'ingredients': Ingredient.objects.all().order_by('name'),
        'ing_filter': ing_id,
    })


# ── Excel Exports ──────────────────────────────────────────────────────────────

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

_ORANGE_HEX = 'FF4500'
_LIGHT_HEX  = 'FFF5F2'
_HEADER_FONT = Font(bold=True, color='FFFFFF', size=10)
_HEADER_FILL = PatternFill('solid', fgColor=_ORANGE_HEX)
_TOTAL_FILL  = PatternFill('solid', fgColor=_LIGHT_HEX)
_TOTAL_FONT  = Font(bold=True, color=_ORANGE_HEX, size=10)
_BORDER = Border(
    left=Side(style='thin', color='E5E7EB'), right=Side(style='thin', color='E5E7EB'),
    top=Side(style='thin', color='E5E7EB'),  bottom=Side(style='thin', color='E5E7EB'),
)
_CENTER = Alignment(horizontal='center', vertical='center')
_LEFT   = Alignment(horizontal='left',   vertical='center')
_RIGHT  = Alignment(horizontal='right',  vertical='center')

def _xl_response(wb, filename):
    from io import BytesIO
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    resp = HttpResponse(buf, content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    resp['Content-Disposition'] = f'attachment; filename="{filename}"'
    return resp

def _xl_header_row(ws, cols):
    """Write a styled header row. cols = [(title, width), ...]"""
    for ci, (title, width) in enumerate(cols, 1):
        c = ws.cell(row=1, column=ci, value=title)
        c.font = _HEADER_FONT; c.fill = _HEADER_FILL
        c.alignment = _CENTER; c.border = _BORDER
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
    _log(request, 'export_excel', 'Financial Report', detail=f"from={date_from_str} to={date_to_str}")

    wb = Workbook()
    ws = wb.active
    ws.title = 'Financial Report'

    # Row 1: Title
    ws.row_dimensions[1].height = 26
    ws.row_dimensions[2].height = 18
    title_c = ws.cell(row=1, column=1,
        value=f'Searing Sandwiches — Financial Report  |  {_period_label(date_from_str, date_to_str)}')
    title_c.font = Font(bold=True, size=13, color=_ORANGE_HEX)
    title_c.alignment = _LEFT

    # Row 2: Headers
    cols = [('Order #',13),('Date',12),('Time',8),('Product',28),
            ('Unit Cost',12),('Sale Price',12),('Qty',6),
            ('Full Cost',13),('Revenue',13),('Profit',13)]
    for ci, (title, width) in enumerate(cols, 1):
        c = ws.cell(row=2, column=ci, value=title)
        c.font = _HEADER_FONT; c.fill = _HEADER_FILL
        c.alignment = _CENTER; c.border = _BORDER
        ws.column_dimensions[get_column_letter(ci)].width = width

    r = 3
    grand_cost = grand_revenue = grand_profit = 0.0
    for order in orders:
        for oi in order.items.all():
            if not oi.menu_item: continue
            unit_cost = sum(float(_convert_to_stock_unit(rec.quantity_required, rec.recipe_unit, rec.ingredient.unit)) * float(rec.ingredient.cost_per_unit)
                            for rec in oi.menu_item.ingredients.all())
            qty = oi.quantity; sale = float(oi.price_at_order)
            fc = unit_cost * qty; fr = sale * qty; prof = fr - fc
            grand_cost += fc; grand_revenue += fr; grand_profit += prof
            for ci, v in enumerate([order.order_number,
                                     order.created_at.strftime('%Y-%m-%d'),
                                     order.created_at.strftime('%H:%M'),
                                     oi.menu_item.name,
                                     round(unit_cost,2), round(sale,2), qty,
                                     round(fc,2), round(fr,2), round(prof,2)], 1):
                fmt = '#,##0.00' if ci in (5,6,8,9,10) else None
                aln = _RIGHT if ci in (5,6,7,8,9,10) else _LEFT
                _xl_data_cell(ws, r, ci, v, num_fmt=fmt, align=aln)
            r += 1

    # Totals row
    for ci, v in enumerate(['TOTAL','','','','','','',
                             round(grand_cost,2), round(grand_revenue,2), round(grand_profit,2)], 1):
        c = _xl_data_cell(ws, r, ci, v, num_fmt='#,##0.00' if ci >= 8 else None,
                          bold=True, fill=_TOTAL_FILL, align=_RIGHT if ci >= 8 else _CENTER)
        if ci >= 8: c.font = _TOTAL_FONT

    # Summary sheet
    ws2 = wb.create_sheet('Summary')
    ws2.column_dimensions['A'].width = 28
    ws2.column_dimensions['B'].width = 18
    monthly_payroll = float(sum(e.salary for e in Employee.objects.filter(is_active=True)))
    net_profit = grand_profit - monthly_payroll
    summary = [('Total Ingredient Cost', grand_cost),
               ('Total Revenue', grand_revenue),
               ('Gross Profit', grand_profit),
               ('Monthly Payroll (deduction)', -monthly_payroll),
               ('Net Profit', net_profit)]
    ws2['A1'].value = 'Summary'; ws2['A1'].font = Font(bold=True, size=12, color=_ORANGE_HEX)
    for i, (label, val) in enumerate(summary, 2):
        c1 = ws2.cell(row=i, column=1, value=label)
        c2 = ws2.cell(row=i, column=2, value=round(val,2))
        c1.border = _BORDER; c2.border = _BORDER
        c2.number_format = '#,##0.00'; c2.alignment = _RIGHT
        if label == 'Net Profit':
            c1.font = Font(bold=True); c2.font = Font(bold=True, color=_ORANGE_HEX)
            c1.fill = _TOTAL_FILL;     c2.fill = _TOTAL_FILL

    return _xl_response(wb, 'financial_report.xlsx')


@manager_required
def excel_report_ingredients(request):
    _log(request, 'export_excel', 'Ingredient Usage Report',
         detail=f"from={request.GET.get('date_from','')} to={request.GET.get('date_to','')}")
    orders, date_from_str, date_to_str = _get_filtered_orders(request)

    item_qty = {}; item_names = {}
    for order in orders:
        for oi in order.items.all():
            if not oi.menu_item: continue
            mid = oi.menu_item_id
            item_qty[mid]   = item_qty.get(mid, 0) + oi.quantity
            item_names[mid] = oi.menu_item

    wb = Workbook()
    ws = wb.active; ws.title = 'Ingredient Usage'

    ws.row_dimensions[1].height = 26
    ws.cell(row=1, column=1, value=f'Searing Sandwiches — Ingredient Usage  |  {_period_label(date_from_str, date_to_str)}').font = Font(bold=True, size=13, color=_ORANGE_HEX)

    cols = [('Product Name',28),('Qty Sold',10),('Ingredient',22),('Qty Used',12),('Unit',10),('Ing. Cost (EGP)',16)]
    for ci, (title, width) in enumerate(cols, 1):
        c = ws.cell(row=3, column=ci, value=title)
        c.font = _HEADER_FONT; c.fill = _HEADER_FILL
        c.alignment = _CENTER; c.border = _BORDER
        ws.column_dimensions[get_column_letter(ci)].width = width

    r = 4
    for mid, menu_item in item_names.items():
        qty_sold = item_qty[mid]
        recipes  = list(menu_item.ingredients.all())
        if not recipes:
            for ci, v in enumerate([menu_item.name, qty_sold, '—','—','—','—'], 1):
                _xl_data_cell(ws, r, ci, v)
            r += 1; continue
        for i, recipe in enumerate(recipes):
            ing = recipe.ingredient
            sq  = float(_convert_to_stock_unit(recipe.quantity_required, recipe.recipe_unit, ing.unit))
            tqu = round(sq * qty_sold, 3)
            ic  = round(tqu * float(ing.cost_per_unit), 2)
            for ci, v in enumerate([menu_item.name if i==0 else '',
                                     qty_sold if i==0 else '',
                                     ing.name, tqu, ing.unit, ic], 1):
                fmt = '#,##0.000' if ci == 4 else ('#,##0.00' if ci == 6 else None)
                aln = _RIGHT if ci in (2,4,6) else _LEFT
                _xl_data_cell(ws, r, ci, v, num_fmt=fmt, align=aln)
            r += 1
        # spacer row
        ws.row_dimensions[r].height = 6; r += 1

    return _xl_response(wb, 'ingredient_usage.xlsx')


@manager_required
def excel_report_sales(request):
    """Excel version of the sales summary report."""
    _log(request, 'export_excel', 'Sales Summary',
         detail=f"from={request.GET.get('date_from','')} to={request.GET.get('date_to','')}")
    from decimal import Decimal
    date_from_str = request.GET.get('date_from', '')
    date_to_str   = request.GET.get('date_to',   '')

    orders = Order.objects.filter(status='active').prefetch_related(
        'items__menu_item__ingredients__ingredient').order_by('created_at')
    if date_from_str:
        try: orders = orders.filter(created_at__date__gte=datetime.strptime(date_from_str,'%Y-%m-%d').date())
        except ValueError: pass
    if date_to_str:
        try: orders = orders.filter(created_at__date__lte=datetime.strptime(date_to_str,'%Y-%m-%d').date())
        except ValueError: pass

    wb = Workbook()

    # ── Orders sheet ──────────────────────────────────────────────────────
    ws_orders = wb.active; ws_orders.title = 'Orders'
    ws_orders.row_dimensions[1].height = 26
    ws_orders.cell(row=1, column=1, value=f'Searing Sandwiches — Orders  |  {_period_label(date_from_str, date_to_str)}').font = Font(bold=True, size=13, color=_ORANGE_HEX)

    o_cols = [('Order #',14),('Date',13),('Time',8),('Table',8),('Discount',12),('Total',14),('Status',10)]
    for ci, (t,w) in enumerate(o_cols, 1):
        c = ws_orders.cell(row=3, column=ci, value=t)
        c.font=_HEADER_FONT; c.fill=_HEADER_FILL; c.alignment=_CENTER; c.border=_BORDER
        ws_orders.column_dimensions[get_column_letter(ci)].width = w

    r = 4; grand_total = Decimal('0')
    for order in orders:
        grand_total += order.total_price
        for ci, v in enumerate([order.order_number,
                                 order.created_at.strftime('%Y-%m-%d'),
                                 order.created_at.strftime('%H:%M'),
                                 order.table_number or '',
                                 float(order.discount_amount),
                                 float(order.total_price),
                                 order.status], 1):
            fmt = '#,##0.00' if ci in (5,6) else None
            aln = _RIGHT if ci in (5,6) else _LEFT
            _xl_data_cell(ws_orders, r, ci, v, num_fmt=fmt, align=aln)
        r += 1
    # Total row
    for ci, v in enumerate(['GRAND TOTAL','','','','',float(grand_total),''], 1):
        c = _xl_data_cell(ws_orders, r, ci, v,
                          num_fmt='#,##0.00' if ci==6 else None,
                          bold=True, fill=_TOTAL_FILL, align=_RIGHT if ci==6 else _CENTER)
        if ci == 6: c.font = _TOTAL_FONT

    # ── Top Items sheet ───────────────────────────────────────────────────
    ws_items = wb.create_sheet('Top Items')
    item_stats = {}
    for order in orders:
        for oi in order.items.all():
            if not oi.menu_item: continue
            n = oi.menu_item.name
            if n not in item_stats:
                item_stats[n] = {'qty': 0, 'revenue': Decimal('0'), 'cost': Decimal('0')}
            item_stats[n]['qty'] += oi.quantity
            item_stats[n]['revenue'] += oi.price_at_order * oi.quantity
            for rec in oi.menu_item.ingredients.all():
                sq = _convert_to_stock_unit(rec.quantity_required, rec.recipe_unit, rec.ingredient.unit)
                item_stats[n]['cost'] += sq * rec.ingredient.cost_per_unit * oi.quantity

    ws_items.row_dimensions[1].height = 26
    ws_items.cell(row=1, column=1, value='Sales by Menu Item').font = Font(bold=True, size=13, color=_ORANGE_HEX)
    i_cols = [('Item',28),('Qty Sold',10),('Revenue (EGP)',16),('COGS (EGP)',14),('Margin %',12)]
    for ci, (t,w) in enumerate(i_cols, 1):
        c = ws_items.cell(row=3, column=ci, value=t)
        c.font=_HEADER_FONT; c.fill=_HEADER_FILL; c.alignment=_CENTER; c.border=_BORDER
        ws_items.column_dimensions[get_column_letter(ci)].width = w
    for ri, (name, d) in enumerate(sorted(item_stats.items(), key=lambda x: -x[1]['qty']), 4):
        rev  = float(d['revenue']); cost = float(d['cost'])
        margin = round((rev-cost)/rev*100, 1) if rev else 0
        for ci, v in enumerate([name, d['qty'], round(rev,2), round(cost,2), margin], 1):
            fmt = '#,##0.00' if ci in (3,4) else ('0.0"%"' if ci==5 else None)
            aln = _RIGHT if ci > 1 else _LEFT
            _xl_data_cell(ws_items, ri, ci, v, num_fmt=fmt, align=aln)

    return _xl_response(wb, 'sales_report.xlsx')


@manager_required
def excel_stock_log(request):
    _log(request, 'export_excel', 'Stock Log')
    logs = StockLog.objects.select_related('ingredient','changed_by').order_by('-created_at')[:1000]

    wb = Workbook(); ws = wb.active; ws.title = 'Stock Log'
    ws.row_dimensions[1].height = 26
    ws.cell(row=1, column=1, value='Searing Sandwiches — Stock Log').font = Font(bold=True, size=13, color=_ORANGE_HEX)

    cols = [('Date/Time',18),('Ingredient',22),('Reason',14),('Before',12),('Change',12),('After',12),('By',16)]
    for ci,(t,w) in enumerate(cols,1):
        c = ws.cell(row=3,column=ci,value=t)
        c.font=_HEADER_FONT; c.fill=_HEADER_FILL; c.alignment=_CENTER; c.border=_BORDER
        ws.column_dimensions[get_column_letter(ci)].width=w

    for ri, log in enumerate(logs, 4):
        for ci, v in enumerate([
            log.created_at.strftime('%Y-%m-%d %H:%M'),
            log.ingredient.name if log.ingredient else '—',
            log.get_reason_display(),
            float(log.qty_before), float(log.qty_change), float(log.qty_after),
            log.changed_by.username if log.changed_by else '—',
        ], 1):
            fmt = '#,##0.000' if ci in (4,5,6) else None
            aln = _RIGHT if ci in (4,5,6) else _LEFT
            _xl_data_cell(ws, ri, ci, v, num_fmt=fmt, align=aln)

    return _xl_response(wb, 'stock_log.xlsx')


@manager_required
def excel_cost_history(request):
    _log(request, 'export_excel', 'Cost History')
    history = CostHistory.objects.select_related('ingredient','recorded_by').order_by('-created_at')[:1000]

    wb = Workbook(); ws = wb.active; ws.title = 'Cost History'
    ws.row_dimensions[1].height = 26
    ws.cell(row=1, column=1, value='Searing Sandwiches — Cost History').font = Font(bold=True, size=13, color=_ORANGE_HEX)

    cols = [('Date/Time',18),('Ingredient',22),('Old Cost',12),('New Cost',12),('Qty Restocked',15),('Total Paid',13),('By',16)]
    for ci,(t,w) in enumerate(cols,1):
        c = ws.cell(row=3,column=ci,value=t)
        c.font=_HEADER_FONT; c.fill=_HEADER_FILL; c.alignment=_CENTER; c.border=_BORDER
        ws.column_dimensions[get_column_letter(ci)].width=w

    for ri, h in enumerate(history, 4):
        for ci, v in enumerate([
            h.created_at.strftime('%Y-%m-%d %H:%M'),
            h.ingredient.name if h.ingredient else '—',
            float(h.old_cost), float(h.new_cost),
            float(h.qty_restocked), float(h.total_paid),
            h.recorded_by.username if h.recorded_by else '—',
        ], 1):
            fmt = '#,##0.0000' if ci in (3,4) else ('#,##0.000' if ci==5 else ('#,##0.00' if ci==6 else None))
            aln = _RIGHT if ci in (3,4,5,6) else _LEFT
            _xl_data_cell(ws, ri, ci, v, num_fmt=fmt, align=aln)

    return _xl_response(wb, 'cost_history.xlsx')


@manager_required
def excel_activity_log(request):
    _log(request, 'export_excel', 'Activity Log')
    logs = ActivityLog.objects.select_related('user').order_by('-created_at')[:2000]

    wb = Workbook(); ws = wb.active; ws.title = 'Activity Log'
    ws.row_dimensions[1].height = 26
    ws.cell(row=1, column=1, value='Searing Sandwiches — Activity Log').font = Font(bold=True, size=13, color=_ORANGE_HEX)

    cols = [('Date/Time',18),('User',16),('Action',24),('Target',28),('Detail',36),('IP Address',16)]
    for ci,(t,w) in enumerate(cols,1):
        c = ws.cell(row=3,column=ci,value=t)
        c.font=_HEADER_FONT; c.fill=_HEADER_FILL; c.alignment=_CENTER; c.border=_BORDER
        ws.column_dimensions[get_column_letter(ci)].width=w

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


# ── Full Data Backup Export ────────────────────────────────────────────────────

@manager_required
def export_full_backup(request):
    """Export every data table as a separate sheet in one Excel file."""
    wb = Workbook()
    ORANGE_HEX = 'FF4500'
    HDR_FONT = Font(bold=True, color='FFFFFF', size=9)
    HDR_FILL = PatternFill('solid', fgColor=ORANGE_HEX)
    HDR_AL   = Alignment(horizontal='center', vertical='center')

    def _sheet(name, headers, rows):
        ws = wb.create_sheet(name)
        ws.append(headers)
        for ci, h in enumerate(headers, 1):
            c = ws.cell(row=1, column=ci)
            c.font = HDR_FONT; c.fill = HDR_FILL; c.alignment = HDR_AL
            ws.column_dimensions[get_column_letter(ci)].width = max(14, len(str(h)) + 4)
        for row in rows:
            ws.append([str(v) if v is not None else '' for v in row])

    _sheet('Orders',
        ['ID','Order#','Date','Total EGP','Discount EGP','Status','Void Reason','Promo'],
        [(o.id, o.order_number, o.created_at.strftime('%Y-%m-%d %H:%M'),
          float(o.total_price), float(o.discount_amount), o.status,
          o.void_reason, o.promo_code.code if o.promo_code else '')
         for o in Order.objects.select_related('promo_code').order_by('-created_at')])

    _sheet('OrderItems',
        ['ID','Order#','Menu Item','Qty','Price At Order EGP','COGS At Sale EGP'],
        [(oi.id, oi.order.order_number,
          oi.menu_item.name if oi.menu_item else '[deleted]',
          oi.quantity, float(oi.price_at_order), float(getattr(oi,'cogs_at_sale',0)))
         for oi in OrderItem.objects.select_related('order','menu_item').order_by('-order__created_at')])

    _sheet('MenuItems',
        ['ID','Name','Category','Price EGP','Active','Description'],
        [(m.id, m.name, m.category.name if m.category else '',
          float(m.price), m.is_active, m.description)
         for m in MenuItem.objects.select_related('category').order_by('name')])

    _sheet('Ingredients',
        ['ID','Name','Unit','Stock Qty','Cost/Unit EGP','Low Stock Threshold'],
        [(i.id, i.name, i.unit, float(i.stock_quantity),
          float(i.cost_per_unit), float(i.low_stock_threshold))
         for i in Ingredient.objects.order_by('name')])

    _sheet('Expenses',
        ['ID','Date','Title','Category','Amount EGP','Notes'],
        [(e.id, str(e.date), e.title, e.get_category_display(),
          float(e.amount), e.notes)
         for e in Expense.objects.order_by('-date')])

    _sheet('Employees',
        ['ID','Full Name','Job','Phone','Salary EGP','Active','Hire Date'],
        [(e.id, e.full_name, e.get_job_title_display(), e.phone,
          float(e.salary), e.is_active, str(e.hire_date))
         for e in Employee.objects.order_by('full_name')])

    _sheet('StockLog',
        ['ID','Date','Ingredient','Reason','Before','Change','After','Note','By'],
        [(l.id, l.created_at.strftime('%Y-%m-%d %H:%M'), l.ingredient.name,
          l.get_reason_display(), float(l.qty_before), float(l.qty_change),
          float(l.qty_after), l.note,
          l.changed_by.username if l.changed_by else '')
         for l in StockLog.objects.select_related('ingredient','changed_by').order_by('-created_at')])

    _sheet('CostHistory',
        ['ID','Date','Ingredient','Old Cost','New Cost','Qty Restocked','Total Paid EGP','By'],
        [(h.id, h.created_at.strftime('%Y-%m-%d %H:%M'), h.ingredient.name,
          float(h.old_cost), float(h.new_cost), float(h.qty_restocked),
          float(h.total_paid), h.recorded_by.username if h.recorded_by else '')
         for h in CostHistory.objects.select_related('ingredient','recorded_by').order_by('-created_at')])

    _sheet('ActivityLog',
        ['ID','Date','User','Action','Target','Detail','IP'],
        [(l.id, l.created_at.strftime('%Y-%m-%d %H:%M'),
          l.user.username if l.user else '',
          l.get_action_display(), l.target, l.detail, l.ip_address or '')
         for l in ActivityLog.objects.select_related('user').order_by('-created_at')])

    if 'Sheet' in wb.sheetnames:
        del wb['Sheet']

    _log(request, 'export_excel', 'Full Data Backup')
    stamp = timezone.now().strftime('%Y%m%d_%H%M')
    return _xl_response(wb, f'searing_backup_{stamp}.xlsx')


# ── Activity Log View ──────────────────────────────────────────────────────────

@manager_required
def activity_log_view(request):
    logs = ActivityLog.objects.select_related('user').order_by('-created_at')

    # Filters
    user_filter   = request.GET.get('user', '')
    action_filter = request.GET.get('action', '')
    date_filter   = request.GET.get('date', '')

    if user_filter:   logs = logs.filter(user__username__icontains=user_filter)
    if action_filter: logs = logs.filter(action=action_filter)
    if date_filter:
        try:
            logs = logs.filter(created_at__date=datetime.strptime(date_filter, '%Y-%m-%d').date())
        except ValueError: pass

    from django.core.paginator import Paginator
    paginator = Paginator(logs, 50)
    page_obj  = paginator.get_page(request.GET.get('page'))
    return render(request, 'core/activity_log.html', {
        'logs':           page_obj,
        'page_obj':       page_obj,
        'action_choices': ActivityLog.ACTION_CHOICES,
        'user_filter':    user_filter,
        'action_filter':  action_filter,
        'date_filter':    date_filter,
    })
