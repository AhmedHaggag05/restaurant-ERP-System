import logging
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.db import transaction
from django.db.models import Sum
from decimal import Decimal, InvalidOperation
from datetime import date

from core.models import Ingredient
from core.views import manager_required
from core.models import StockLog, CostHistory
from .models import Supplier, PurchaseOrder, PurchaseOrderLine, SupplierInvoice

logger = logging.getLogger(__name__)


# ── Suppliers ─────────────────────────────────────────────────────────────

@manager_required
def suppliers(request):
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'add':
            name = request.POST.get('name', '').strip()[:150]
            if not name:
                messages.error(request, "Supplier name required.")
            elif Supplier.objects.filter(name__iexact=name).exists():
                messages.error(request, f"'{name}' already exists.")
            else:
                Supplier.objects.create(
                    name=name,
                    contact=request.POST.get('contact', '').strip()[:100],
                    phone=request.POST.get('phone', '').strip()[:30],
                    email=request.POST.get('email', '').strip()[:254],
                    address=request.POST.get('address', '').strip()[:500],
                    notes=request.POST.get('notes', '').strip()[:1000],
                )
                messages.success(request, f"Supplier '{name}' added.")
        elif action == 'edit':
            sup = get_object_or_404(Supplier, id=request.POST.get('supplier_id'))
            sup.name    = request.POST.get('name', sup.name).strip()[:150]
            sup.contact = request.POST.get('contact', sup.contact).strip()[:100]
            sup.phone   = request.POST.get('phone', sup.phone).strip()[:30]
            sup.email   = request.POST.get('email', sup.email).strip()[:254]
            sup.address = request.POST.get('address', sup.address).strip()[:500]
            sup.notes   = request.POST.get('notes', sup.notes).strip()[:1000]
            sup.is_active = request.POST.get('is_active') == 'on'
            sup.save()
            messages.success(request, f"Supplier '{sup.name}' updated.")
        elif action == 'delete':
            sup = get_object_or_404(Supplier, id=request.POST.get('supplier_id'))
            sup.delete()
            messages.success(request, "Supplier deleted.")
        return redirect('purchasing:suppliers')

    # Build supplier scorecards
    raw_suppliers = Supplier.objects.prefetch_related('purchase_orders__lines').order_by('name')
    supplier_data = []
    for sup in raw_suppliers:
        pos = list(sup.purchase_orders.all())
        total_pos   = len(pos)
        total_spend = sum(po.total_amount for po in pos)
        received_pos = [po for po in pos if po.status in ('received', 'partial')]
        on_time = sum(
            1 for po in received_pos
            if po.expected_date and po.received_date and po.received_date <= po.expected_date
        )
        with_expected = sum(1 for po in received_pos if po.expected_date and po.received_date)
        on_time_pct = round(on_time / with_expected * 100) if with_expected else None
        fill_rates = []
        for po in received_pos:
            for line in po.lines.all():
                if line.quantity_ordered > 0:
                    fill_rates.append(float(line.quantity_received) / float(line.quantity_ordered))
        avg_fill = round(sum(fill_rates) / len(fill_rates) * 100) if fill_rates else None
        supplier_data.append({
            'supplier':    sup,
            'total_pos':   total_pos,
            'total_spend': total_spend,
            'on_time_pct': on_time_pct,
            'avg_fill':    avg_fill,
        })
    return render(request, 'purchasing/suppliers.html', {'suppliers': supplier_data})


# ── Purchase Orders ───────────────────────────────────────────────────────

@manager_required
def purchase_orders(request):
    pos = PurchaseOrder.objects.select_related('supplier').prefetch_related('lines').all()
    suppliers_qs = Supplier.objects.filter(is_active=True).order_by('name')
    ingredients = Ingredient.objects.all().order_by('name')
    return render(request, 'purchasing/purchase_orders.html', {
        'purchase_orders': pos,
        'suppliers': suppliers_qs,
        'ingredients': ingredients,
    })


@manager_required
def create_purchase_order(request):
    if request.method == 'POST':
        supplier_id   = request.POST.get('supplier_id')
        expected_date = request.POST.get('expected_date') or None
        notes         = request.POST.get('notes', '').strip()[:1000]
        supplier = get_object_or_404(Supplier, id=supplier_id)

        try:
            with transaction.atomic():
                po = PurchaseOrder.objects.create(
                    supplier=supplier,
                    order_date=date.today(),
                    expected_date=expected_date,
                    notes=notes,
                    created_by=request.user,
                )

                ing_ids    = request.POST.getlist('ingredient_id')
                quantities = request.POST.getlist('quantity')
                unit_costs = request.POST.getlist('unit_cost')

                total = Decimal('0')
                skipped_rows = []
                for idx, (ing_id, qty, uc) in enumerate(zip(ing_ids, quantities, unit_costs), start=1):
                    if not ing_id:
                        continue
                    try:
                        ing = Ingredient.objects.get(id=ing_id)
                    except Ingredient.DoesNotExist:
                        skipped_rows.append(f"row {idx} (ingredient not found)")
                        continue
                    try:
                        qty_d = Decimal(qty or '0')
                        uc_d  = Decimal(uc or '0')
                    except InvalidOperation:
                        skipped_rows.append(f"row {idx} ({ing.name}: invalid quantity/cost)")
                        continue
                    if qty_d <= 0 or uc_d < 0:
                        skipped_rows.append(f"row {idx} ({ing.name}: quantity/cost out of range)")
                        continue
                    # Sanity cap: no single line > 999,999 units
                    if qty_d > 999999 or uc_d > 9999999:
                        # Raise (rather than return) so the whole transaction —
                        # including the PO row already created above — rolls back.
                        raise ValueError(f"Unreasonable quantity or cost for {ing.name}.")
                    line = PurchaseOrderLine.objects.create(
                        purchase_order=po,
                        ingredient=ing,
                        quantity_ordered=qty_d,
                        unit_cost=uc_d,
                    )
                    total += line.total_cost

                po.total_amount = total
                po.save()
        except ValueError as e:
            messages.error(request, str(e))
            return redirect('purchasing:create_po')

        if skipped_rows:
            messages.warning(
                request,
                "Some rows were skipped: " + "; ".join(skipped_rows)
            )
        messages.success(request, f"Purchase Order {po.po_number} created.")
        return redirect('purchasing:po_detail', pk=po.pk)

    suppliers_qs = Supplier.objects.filter(is_active=True).order_by('name')
    ingredients  = Ingredient.objects.all().order_by('name')
    return render(request, 'purchasing/create_po.html', {
        'suppliers': suppliers_qs, 'ingredients': ingredients,
    })


@manager_required
def po_detail(request, pk):
    po    = get_object_or_404(PurchaseOrder, pk=pk)
    lines = po.lines.select_related('ingredient').all()

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'update_status':
            new_status = request.POST.get('status')
            if new_status in dict(PurchaseOrder.STATUS_CHOICES):
                po.status = new_status
                if new_status == 'received':
                    po.received_date = date.today()
                po.save()
                messages.success(request, f"Status updated to {po.get_status_display()}.")

        elif action == 'receive':
            with transaction.atomic():
                # Lock this PO's lines up front so two concurrent "receive" submissions
                # for the same PO can't both act on stale quantity_received values.
                locked_lines = list(
                    PurchaseOrderLine.objects.select_for_update()
                    .select_related('ingredient').filter(purchase_order=po)
                )
                any_received = False
                for line in locked_lines:
                    received_key = f'received_{line.id}'
                    try:
                        rec_qty = Decimal(request.POST.get(received_key, '0') or '0')
                    except InvalidOperation:
                        rec_qty = Decimal('0')
                    if rec_qty <= 0:
                        continue
                    # Cap received qty to what was ordered
                    remaining = line.quantity_ordered - line.quantity_received
                    rec_qty = min(rec_qty, remaining)
                    if rec_qty <= 0:
                        continue
                    any_received = True
                    prev_received = line.quantity_received
                    line.quantity_received = prev_received + rec_qty
                    line.save()

                    if not line.ingredient_id:
                        continue

                    # Lock the ingredient row for this read-modify-write so a
                    # concurrent restock (another PO) or sale (create_order deducting
                    # stock) can't interleave and silently lose one side's update —
                    # the same class of bug already fixed in core.views.create_order.
                    ing = Ingredient.objects.select_for_update().get(id=line.ingredient_id)
                    old_qty  = ing.stock_quantity
                    old_cost = ing.cost_per_unit

                    if ing.unit == 'boxes' and ing.units_per_box > 0:
                        upb           = Decimal(str(ing.units_per_box))
                        rec_qty_stock = rec_qty * upb
                        cpu_stock     = line.unit_cost / upb
                    else:
                        rec_qty_stock = rec_qty
                        cpu_stock     = line.unit_cost

                    new_qty    = old_qty + rec_qty_stock
                    total_cost = rec_qty_stock * cpu_stock
                    avg_cost   = (
                        (old_qty * old_cost + rec_qty_stock * cpu_stock) / new_qty
                    ).quantize(Decimal('0.0001')) if new_qty else cpu_stock

                    ing.stock_quantity = new_qty
                    ing.cost_per_unit  = avg_cost
                    ing.save()

                    StockLog.objects.create(
                        ingredient=ing, changed_by=request.user, reason='restock',
                        qty_before=old_qty, qty_change=rec_qty_stock, qty_after=new_qty,
                        note=f'PO {po.po_number}'
                    )
                    CostHistory.objects.create(
                        ingredient=ing, old_cost=old_cost, new_cost=avg_cost,
                        qty_restocked=rec_qty_stock, total_paid=total_cost,
                        recorded_by=request.user,
                    )

                if not any_received:
                    messages.warning(request, "No quantities entered. Nothing was received.")
                    return redirect('purchasing:po_detail', pk=pk)

                all_received = all(
                    line.quantity_received >= line.quantity_ordered for line in locked_lines
                )
                po.status = 'received' if all_received else 'partial'
                po.received_date = date.today()
                po.save()

                if not po.invoices.exists():
                    SupplierInvoice.objects.create(
                        purchase_order=po, supplier=po.supplier,
                        amount=po.total_amount, created_by=request.user,
                    )
                    try:
                        from accounting.utils import post_purchase_journal
                        post_purchase_journal(po, request.user)
                    except Exception:
                        logger.exception(
                            "Failed to post purchase journal for PO %s", po.po_number)
                        messages.warning(
                            request,
                            "Goods received, but the accounting journal entry failed "
                            "to post automatically. Please check the Accounting module."
                        )

            status_label = "fully received" if all_received else "partially received"
            messages.success(request, f"Goods {status_label}. Stock updated.")
        return redirect('purchasing:po_detail', pk=pk)

    return render(request, 'purchasing/po_detail.html', {'po': po, 'lines': lines})


# ── Supplier Invoices ─────────────────────────────────────────────────────

@manager_required
def delete_po(request, pk):
    po = get_object_or_404(PurchaseOrder, pk=pk)
    if request.method != 'POST':
        return redirect('purchasing:purchase_orders')
    po_number = po.po_number
    try:
        po.delete()
        messages.success(request, f"Purchase Order {po_number} deleted.")
    except Exception:
        logger.exception("Failed to delete PO %s", po_number)
        messages.error(request, f"Could not delete {po_number}.")
    return redirect('purchasing:purchase_orders')


@manager_required
def invoices(request):
    if request.method == 'POST':
        action = request.POST.get('action')
        inv = get_object_or_404(SupplierInvoice, id=request.POST.get('invoice_id'))
        if action == 'mark_paid':
            inv.amount_paid = inv.amount
            inv.status = 'paid'
            inv.save()
            messages.success(request, "Invoice marked as paid.")
        elif action == 'partial_pay':
            try:
                paid = Decimal(request.POST.get('paid_amount', '0') or '0')
            except InvalidOperation:
                paid = Decimal('0')
            paid = max(paid, Decimal('0'))
            inv.amount_paid = min(paid, inv.amount)
            inv.status = 'paid' if inv.amount_paid >= inv.amount else 'partial'
            inv.save()
            messages.success(request, "Payment recorded.")
        return redirect('purchasing:invoices')

    all_inv = SupplierInvoice.objects.select_related('supplier', 'purchase_order').all()
    totals = all_inv.aggregate(total=Sum('amount'), paid=Sum('amount_paid'))
    return render(request, 'purchasing/invoices.html', {
        'invoices': all_inv, 'totals': totals,
    })