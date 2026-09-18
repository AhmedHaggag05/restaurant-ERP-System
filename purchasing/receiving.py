"""Receive purchase order goods and post the matching accounting value."""

from datetime import date
from decimal import Decimal, InvalidOperation

from django.db import transaction

from accounting.utils import post_purchase_journal
from core.models import CostHistory, Ingredient, StockLog
from .models import PurchaseOrder, PurchaseOrderLine, SupplierInvoice


@transaction.atomic
def receive_goods(po_id, submitted, user):
    po = PurchaseOrder.objects.select_for_update().get(pk=po_id)
    if po.status in ('cancelled', 'received'):
        raise ValueError('This purchase order cannot receive more goods.')
    lines = list(PurchaseOrderLine.objects.select_for_update().filter(purchase_order=po))
    received_value = Decimal('0')
    received_any = False

    for line in lines:
        raw = submitted.get(f'received_{line.pk}', '0') or '0'
        try:
            received = Decimal(raw)
        except (InvalidOperation, TypeError):
            raise ValueError('Received quantities must be valid numbers.')
        if not received.is_finite() or received < 0:
            raise ValueError('Received quantities cannot be negative or infinite.')
        if received != received.quantize(Decimal('0.001')):
            raise ValueError('Received quantities support at most three decimal places.')
        if received == 0:
            continue
        remaining = line.quantity_ordered - line.quantity_received
        if received > remaining:
            raise ValueError('Received quantity exceeds the amount remaining on the purchase order.')
        if not line.ingredient_id:
            raise ValueError('A purchase order line has no ingredient. Restore it before receiving.')

        ingredient = Ingredient.objects.select_for_update().get(pk=line.ingredient_id)
        old_quantity = ingredient.stock_quantity
        old_cost = ingredient.cost_per_unit
        if old_quantity < 0 or line.unit_cost < 0:
            raise ValueError('Ingredient stock or purchase cost is invalid. Reconcile it before receiving.')
        if ingredient.unit == 'boxes':
            units_per_box = Decimal(ingredient.units_per_box)
            if units_per_box <= 0:
                raise ValueError('Ingredient units per box must be positive.')
            stock_received = received * units_per_box
            stock_unit_cost = line.unit_cost / units_per_box
        else:
            stock_received = received
            stock_unit_cost = line.unit_cost
        new_quantity = old_quantity + stock_received
        new_cost = ((old_quantity * old_cost + stock_received * stock_unit_cost)
                    / new_quantity).quantize(Decimal('0.0001'))
        ingredient.stock_quantity = new_quantity
        ingredient.cost_per_unit = new_cost
        ingredient.save(update_fields=['stock_quantity', 'cost_per_unit'])
        old_received = line.quantity_received
        line.quantity_received += received
        line.save(update_fields=['quantity_received'])
        # Delta of rounded cumulative value makes split receipts add up exactly
        # to the value of a fully received purchase-order line.
        line_value = ((line.quantity_received * line.unit_cost).quantize(Decimal('0.01')) -
                      (old_received * line.unit_cost).quantize(Decimal('0.01')))
        received_value += line_value
        StockLog.objects.create(
            ingredient=ingredient, changed_by=user, reason='restock',
            qty_before=old_quantity, qty_change=stock_received,
            qty_after=new_quantity, note=f'PO {po.po_number}',
        )
        CostHistory.objects.create(
            ingredient=ingredient, old_cost=old_cost, new_cost=new_cost,
            qty_restocked=stock_received, total_paid=line_value,
            recorded_by=user,
        )
        received_any = True

    if not received_any:
        raise ValueError('No quantities entered. Nothing was received.')
    all_received = all(line.quantity_received >= line.quantity_ordered for line in lines)
    po.status = 'received' if all_received else 'partial'
    po.received_date = date.today()
    po.save(update_fields=['status', 'received_date'])
    received_value = received_value.quantize(Decimal('0.01'))
    if received_value > 0:
        SupplierInvoice.objects.create(
            purchase_order=po, supplier=po.supplier,
            amount=received_value, created_by=user,
        )
        post_purchase_journal(po, user, amount=received_value)
    return po, all_received, received_value
