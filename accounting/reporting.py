"""Posted journal totals used by manager financial summaries."""

from datetime import date
from decimal import Decimal

from django.db.models import Sum

from .models import JournalLine


def _date_or_none(value):
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def profit_loss_totals(date_from=None, date_to=None):
    lines = JournalLine.objects.filter(
        journal_entry__is_posted=True,
        account__account_type__in=('revenue', 'expense'),
    )
    start, end = _date_or_none(date_from), _date_or_none(date_to)
    if start:
        lines = lines.filter(journal_entry__date__gte=start)
    if end:
        lines = lines.filter(journal_entry__date__lte=end)
    totals = {}
    types = {}
    for row in lines.values('account__code', 'account__account_type', 'side').annotate(amount=Sum('amount')):
        code = row['account__code']
        types[code] = row['account__account_type']
        sign = (1 if row['side'] == 'credit' else -1)
        if row['account__account_type'] == 'expense':
            sign = -sign
        totals[code] = totals.get(code, Decimal('0')) + sign * row['amount']
    revenue_codes = {code for code in totals if types[code] == 'revenue'}
    expense_codes = {code for code in totals if types[code] == 'expense'}
    revenue = sum((totals[code] for code in revenue_codes), Decimal('0'))
    expenses = sum((totals[code] for code in expense_codes), Decimal('0'))
    sales = totals.get('4000', Decimal('0'))
    cogs = totals.get('5000', Decimal('0'))
    payroll = totals.get('5100', Decimal('0'))
    operating = totals.get('5200', Decimal('0'))
    damage = totals.get('5400', Decimal('0'))
    return {
        'sales': sales,
        'cogs': cogs,
        'payroll': payroll,
        'operating': operating,
        'damage': damage,
        'other_revenue': revenue - sales,
        'other_expenses': expenses - cogs - payroll - operating - damage,
        'gross_profit': sales - cogs,
        'net_profit': revenue - expenses,
    }
