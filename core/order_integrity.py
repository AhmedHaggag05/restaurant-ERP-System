"""Stock and journal changes for edits and voids of existing orders."""

from decimal import Decimal

from django.db import transaction
from django.db.models import F, Q, Sum
from django.utils import timezone

from accounting.utils import post_order_adjustment_journal, post_void_journal
from .models import Ingredient, Order, OrderItem, OrderItemConsumption, PromoCode, StockLog


STOCK_PRECISION = Decimal('0.001')
COGS_PRECISION = Decimal('0.0001')


def _stock_units(quantity, recipe_unit, stock_unit):
    quantity = Decimal(quantity)
    if (recipe_unit, stock_unit) in (('g', 'kg'), ('mL', 'L')):
        quantity /= Decimal('1000')
    return quantity.quantize(STOCK_PRECISION)


def _change_stock(ingredient_id, change, order, user, reason):
    if not change:
        return
    ingredient = Ingredient.objects.select_for_update().get(pk=ingredient_id)
    before = ingredient.stock_quantity
    after = before + change
    if after < 0:
        raise ValueError(f"Insufficient stock for {ingredient.name}.")
    ingredient.stock_quantity = after
    ingredient.save(update_fields=['stock_quantity'])
    StockLog.objects.create(
        ingredient=ingredient, changed_by=user, reason=reason,
        qty_before=before, qty_change=change, qty_after=after,
        note=f'Order {order.order_number}' if reason == 'sale' else f'Order edit/void {order.order_number}',
    )
    return ingredient


@transaction.atomic
def edit_existing_order(order_id, post, user):
    order = Order.objects.select_for_update().get(pk=order_id)
    if order.status != 'active':
        raise ValueError('Cannot edit a voided order.')

    old_total = order.total_price
    old_cogs = order.items.aggregate(total=Sum('cogs_at_sale'))['total'] or Decimal('0')
    changes = []
    table = str(post.get('table_number', '')).strip()[:10]
    notes = str(post.get('customer_notes', '')).strip()[:500]
    if table != order.table_number:
        order.table_number = table
        changes.append('table changed')
    if notes != order.customer_notes:
        order.customer_notes = notes
        changes.append('notes changed')

    items = list(OrderItem.objects.select_for_update().filter(order=order)
                 .select_related('menu_item'))
    for item in items:
        key = f'qty_{item.pk}'
        if key not in post:
            continue
        try:
            quantity = int(str(post[key]).strip())
        except (TypeError, ValueError):
            raise ValueError('Item quantities must be whole numbers.')
        if quantity < 0 or quantity > 99:
            raise ValueError('Item quantity must be between 0 and 99.')
        if quantity == item.quantity:
            continue
        if not item.stock_snapshot_ready:
            raise ValueError('This older order has no per-item stock snapshot. Its quantities cannot be edited safely; void it instead.')

        old_quantity = item.quantity
        if quantity > old_quantity:
            if item.menu_item_id is None:
                raise ValueError('Cannot increase an item removed from the menu.')
            extra = quantity - old_quantity
            added_cogs = Decimal('0')
            for recipe in item.menu_item.ingredients.select_related('ingredient').all():
                needed = _stock_units(recipe.quantity_required * extra,
                                      recipe.recipe_unit, recipe.ingredient.unit)
                if needed <= 0:
                    continue
                ingredient = _change_stock(recipe.ingredient_id, -needed, order, user, 'edit')
                added_cogs += needed * ingredient.cost_per_unit
                snapshot, _ = OrderItemConsumption.objects.get_or_create(
                    order_item=item, ingredient=ingredient,
                    defaults={'quantity_consumed': Decimal('0')})
                snapshot.quantity_consumed += needed
                snapshot.save(update_fields=['quantity_consumed'])
            item.cogs_at_sale = (item.cogs_at_sale + added_cogs).quantize(COGS_PRECISION)
            item.quantity = quantity
            item.save(update_fields=['quantity', 'cogs_at_sale'])
        else:
            removed = old_quantity - quantity
            for snapshot in item.consumptions.select_related('ingredient').all():
                restored = (snapshot.quantity_consumed if quantity == 0 else
                            (snapshot.quantity_consumed * removed / old_quantity)
                            .quantize(STOCK_PRECISION))
                _change_stock(snapshot.ingredient_id, restored, order, user, 'edit')
                snapshot.quantity_consumed -= restored
                if quantity == 0 or snapshot.quantity_consumed == 0:
                    snapshot.delete()
                else:
                    snapshot.save(update_fields=['quantity_consumed'])
            if quantity == 0:
                item.delete()
            else:
                item.cogs_at_sale = (item.cogs_at_sale * quantity / old_quantity).quantize(COGS_PRECISION)
                item.quantity = quantity
                item.save(update_fields=['quantity', 'cogs_at_sale'])
        changes.append(f'item {item.pk} quantity {old_quantity} to {quantity}')

    remaining = list(OrderItem.objects.filter(order=order))
    if not remaining:
        raise ValueError('An order cannot have no items. Void the order instead.')
    subtotal = sum((item.price_at_order * item.quantity for item in remaining), Decimal('0'))
    order.discount_amount = min(order.discount_amount, subtotal)
    order.total_price = subtotal - order.discount_amount
    order.save(update_fields=['table_number', 'customer_notes', 'discount_amount', 'total_price'])
    new_cogs = sum((item.cogs_at_sale for item in remaining), Decimal('0'))
    post_order_adjustment_journal(
        order, order.total_price - old_total,
        new_cogs.quantize(Decimal('0.01')) - old_cogs.quantize(Decimal('0.01')),
        user,
    )
    return order, changes


@transaction.atomic
def void_existing_order(order_id, reason, user):
    order = Order.objects.select_for_update().get(pk=order_id)
    if order.status != 'active':
        raise ValueError('Order is already voided.')
    items = list(order.items.prefetch_related('consumptions').all())
    if all(item.stock_snapshot_ready for item in items):
        for item in items:
            for snapshot in item.consumptions.all():
                _change_stock(snapshot.ingredient_id, snapshot.quantity_consumed,
                              order, user, 'void')
    else:
        # Old orders have no per-item snapshot. Their stock log records the actual
        # sale and later edits, so never substitute the current menu recipe.
        movements = (StockLog.objects.filter(
            Q(reason='sale', note=f'Order {order.order_number}') |
            Q(reason='edit', note__contains=order.order_number))
            .values('ingredient_id').annotate(total=Sum('qty_change')))
        movements = list(movements)
        if not movements and any(item.cogs_at_sale > 0 for item in items):
            raise ValueError('No historical stock movements were found. This order needs manual reconciliation.')
        for movement in movements:
            restore = -movement['total']
            if restore < 0:
                raise ValueError('Historical stock movements are inconsistent. This order needs manual reconciliation.')
            _change_stock(movement['ingredient_id'], restore, order, user, 'void')

    order.status = 'voided'
    order.voided_at = timezone.now()
    order.voided_by = user
    order.void_reason = reason
    order.save(update_fields=['status', 'voided_at', 'voided_by', 'void_reason'])
    if order.promo_code_id:
        PromoCode.objects.filter(pk=order.promo_code_id, times_used__gt=0).update(
            times_used=F('times_used') - 1)
    post_void_journal(order, user)
    return order
