"""Historical order values for operational item summaries."""

from decimal import Decimal


def line_cogs(item):
    """Use the recorded sale cost, never a later recipe or ingredient price."""
    return item.cogs_at_sale or Decimal('0')


def net_line_revenues(order, items):
    """Allocate an order's discount to its lines, preserving its exact net total."""
    gross = sum((item.price_at_order * item.quantity for item in items), Decimal('0'))
    if gross <= 0:
        return {item.pk: Decimal('0') for item in items}
    result = {}
    allocated = Decimal('0')
    for item in items[:-1]:
        amount = (item.price_at_order * item.quantity * order.total_price / gross).quantize(Decimal('0.01'))
        result[item.pk] = amount
        allocated += amount
    if items:
        result[items[-1].pk] = order.total_price - allocated
    return result
