from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.db.models import Sum
from decimal import Decimal
from datetime import date

from core.models import Ingredient
from core.views import manager_required
from core.models import StockLog, CostHistory
from .models import Supplier, PurchaseOrder, PurchaseOrderLine, SupplierInvoice


# ── Suppliers ─────────────────────────────────────────────────────────────

@manager_required
def suppliers(request):
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'add':
            name = request.POST.get('name', '').strip()
            if not name:
                messages.error(request, "Supplier name required.")
            elif Supplier.objects.filter(name__iexact=name).exists():
                messages.error(request, f"'{name}' already exists.")
            else:
                Supplier.objects.create(
                    name=name,
                    contact=request.POST.get('contact', '').strip(),
                    phone=request.POST.get('phone', '').strip(),
                    email=request.POST.get('email', '').strip(),
                    address=request.POST.get('address', '').strip(),
                    notes=request.POST.get('notes', '').strip(),
                )
                messages.success(request, f"Supplier '{name}' added.")
        elif action == 'edit':
            sup = get_object_or_404(Supplier, id=request.POST.get('supplier_id'))
            sup.name    = request.POST.get('name', sup.name).strip()
            sup.contact = request.POST.get('contact', sup.contact).strip()
            sup.phone   = request.POST.get('phone', sup.phone).strip()
            sup.email   = request.POST.get('email', sup.email).strip()
            sup.address = request.POST.get('address', sup.address).strip()
            sup.notes   = request.POST.get('notes', sup.notes).strip()
            sup.is_active = request.POST.get('is_active') == 'on'
            sup.save()
            messages.success(request, f"Supplier '{sup.name}' updated.")
        elif action == 'delete':
            sup = get_object_or_404(Supplier, id=request.POST.get('supplier_id'))
            sup.delete()
            messages.success(request, "Supplier deleted.")
        return redirect('purchasing:suppliers')

    # Build supplier scorecards
    from django.db.models import Count, Avg, Q
    from decimal import Decimal
    raw_suppliers = Supplier.objects.prefetch_related('purchase_orders__lines').order_by('name')
    supplier_data = []
    for sup in raw_suppliers:
        pos = list(sup.purchase_orders.all())
        total_pos     = len(pos)
        total_spend   = sum(po.total_amount for po in pos)
        received_pos  = [po for po in pos if po.status in ('received', 'partial')]
        # On-time: received on or before expected date
        on_time = sum(
            1 for po in received_pos
            if po.expected_date and po.received_date and po.received_date <= po.expected_date
        )
        with_expected = sum(1 for po in received_pos if po.expected_date and po.received_date)
        on_time_pct = round(on_time / with_expected * 100) if with_expected else None
        # Average fulfilment rate
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
    suppliers = Supplier.objects.filter(is_active=True).order_by('name')
    ingredients = Ingredient.objects.all().order_by('name')
    return render(request, 'purchasing/purchase_orders.html', {
        'purchase_orders': pos, 'suppliers': suppliers, 'ingredients': ingredients,
    })


@manager_required
def create_purchase_order(request):
    if request.method == 'POST':
        supplier_id   = request.POST.get('supplier_id')
        expected_date = request.POST.get('expected_date') or None
        notes         = request.POST.get('notes', '').strip()
        supplier = get_object_or_404(Supplier, id=supplier_id)

        po = PurchaseOrder.objects.create(
            supplier=supplier,
            order_date=date.today(),
            expected_date=expected_date,
            notes=notes,
            created_by=request.user,
        )

        # Parse line items
        # NOTE: 'quantity' and 'unit_cost' are hidden fields populated by JS,
        # always in the ingredient's main stock unit (kg, L, pieces, boxes).
        # 'qty_entry' and 'unit_cost_entry' are the raw user inputs — ignored server-side.
        ing_ids    = request.POST.getlist('ingredient_id')
        quantities = request.POST.getlist('quantity')
        unit_costs = request.POST.getlist('unit_cost')

        total = Decimal('0')
        for ing_id, qty, uc in zip(ing_ids, quantities, unit_costs):
            if not ing_id:
                continue
            try:
                ing   = Ingredient.objects.get(id=ing_id)
                qty_d = Decimal(qty or '0')
                uc_d  = Decimal(uc  or '0')
                if qty_d <= 0:
                    continue
                line = PurchaseOrderLine.objects.create(
                    purchase_order=po,
                    ingredient=ing,
                    quantity_ordered=qty_d,
                    unit_cost=uc_d,
                )
                total += line.total_cost  # use the model's computed total
            except Exception:
                continue

        po.total_amount = total
        po.save()
        messages.success(request, f"Purchase Order {po.po_number} created.")
        return redirect('purchasing:po_detail', pk=po.pk)

    suppliers   = Supplier.objects.filter(is_active=True).order_by('name')
    ingredients = Ingredient.objects.all().order_by('name')
    return render(request, 'purchasing/create_po.html', {
        'suppliers': suppliers, 'ingredients': ingredients,
    })


@manager_required
def po_detail(request, pk):
    po = get_object_or_404(PurchaseOrder, pk=pk)
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
            # Receive goods per line — additive (can receive in multiple partial shipments)
            any_received = False
            for line in lines:
                received_key = f'received_{line.id}'
                rec_qty = Decimal(request.POST.get(received_key, '0') or '0')
                if rec_qty <= 0:
                    continue
                any_received = True
                # Accumulate — don't overwrite previous receipts
                prev_received     = line.quantity_received
                line.quantity_received = prev_received + rec_qty
                line.save()

                ing = line.ingredient
                old_qty  = ing.stock_quantity
                old_cost = ing.cost_per_unit

                # For box ingredients: PO quantity is in BOXES but stock is in sub-units (cans/packets)
                # Convert both quantity and unit cost to sub-unit basis before updating stock
                if ing.unit == 'boxes' and ing.units_per_box > 0:
                    upb           = Decimal(str(ing.units_per_box))
                    rec_qty_stock = rec_qty * upb          # boxes → cans/packets
                    cpu_stock     = line.unit_cost / upb   # cost/box → cost/can
                else:
                    rec_qty_stock = rec_qty
                    cpu_stock     = line.unit_cost

                new_qty    = old_qty + rec_qty_stock
                total_cost = rec_qty_stock * cpu_stock
                avg_cost   = ((old_qty * old_cost + rec_qty_stock * cpu_stock) / new_qty).quantize(Decimal('0.0001')) if new_qty else cpu_stock

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
                    qty_restocked=rec_qty_stock, total_paid=total_cost, recorded_by=request.user
                )

            if not any_received:
                messages.warning(request, "No quantities entered. Nothing was received.")
                return redirect('purchasing:po_detail', pk=pk)

            # Refresh lines after saves
            lines = po.lines.select_related('ingredient').all()
            all_received = all(line.quantity_received >= line.quantity_ordered for line in lines)
            po.status = 'received' if all_received else 'partial'
            po.received_date = date.today()
            po.save()

            # Only create invoice if not already created for this PO
            if not po.invoices.exists():
                SupplierInvoice.objects.create(
                    purchase_order=po, supplier=po.supplier,
                    amount=po.total_amount, created_by=request.user
                )
                try:
                    from accounting.utils import post_purchase_journal
                    post_purchase_journal(po, request.user)
                except Exception:
                    pass

            status_label = "fully received" if all_received else "partially received"
            messages.success(request, f"Goods {status_label}. Stock updated.")
        return redirect('purchasing:po_detail', pk=pk)

    return render(request, 'purchasing/po_detail.html', {'po': po, 'lines': lines})


# ── Supplier Invoices ─────────────────────────────────────────────────────

@manager_required
def delete_po(request, pk):
    """Delete a Purchase Order and all its lines."""
    from django.shortcuts import get_object_or_404, redirect
    from django.contrib import messages
    po = get_object_or_404(PurchaseOrder, pk=pk)
    if request.method != 'POST':
        return redirect('purchasing:purchase_orders')
    po_number = po.po_number
    try:
        po.delete()
        messages.success(request, f"Purchase Order {po_number} deleted.")
    except Exception as e:
        messages.error(request, f"Could not delete {po_number}: {str(e)}")
    return redirect('purchasing:purchase_orders')


def invoices(request):
    if request.method == 'POST':
        action = request.POST.get('action')
        inv = get_object_or_404(SupplierInvoice, id=request.POST.get('invoice_id'))
        if action == 'mark_paid':
            inv.amount_paid = inv.amount
            inv.status = 'paid'
            inv.save()
            messages.success(request, f"Invoice marked as paid.")
        elif action == 'partial_pay':
            paid = Decimal(request.POST.get('paid_amount', '0') or '0')
            inv.amount_paid = min(paid, inv.amount)
            inv.status = 'paid' if inv.amount_paid >= inv.amount else 'partial'
            inv.save()
            messages.success(request, "Payment recorded.")
        return redirect('purchasing:invoices')

    all_inv = SupplierInvoice.objects.select_related('supplier', 'purchase_order').all()
    totals = all_inv.aggregate(
        total=Sum('amount'), paid=Sum('amount_paid')
    )
    return render(request, 'purchasing/invoices.html', {
        'invoices': all_inv, 'totals': totals,
    })
