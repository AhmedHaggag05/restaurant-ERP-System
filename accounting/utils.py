"""
Accounting utility functions.
These post automatic double-entry journal entries for key business events.

Default Chart of Accounts used:
  1000 - Cash / Bank (Asset)
  1100 - Accounts Receivable (Asset)
  1200 - Inventory / Stock (Asset)
  2000 - Accounts Payable (Liability)
  3000 - Retained Earnings (Equity)
  4000 - Sales Revenue (Revenue)
  5000 - Cost of Goods Sold (Expense)
  5100 - Payroll Expense (Expense)
  5200 - Operating Expenses (Expense)
  5300 - Employee Advances (Asset)
"""
from decimal import Decimal
from django.db.models import Sum
from django.utils import timezone as _tz
from .models import Account, JournalEntry, JournalLine

def _get_today():
    return _tz.now().date()


def _get_or_create_account(code, name, account_type):
    acc, _ = Account.objects.get_or_create(
        code=code,
        defaults={'name': name, 'account_type': account_type, 'is_system': True}
    )
    return acc


def _ensure_coa():
    """Create default chart of accounts if not present."""
    defaults = [
        ('1000', 'Cash / Bank',             'asset'),
        ('1100', 'Accounts Receivable',     'asset'),
        ('1200', 'Inventory / Stock',       'asset'),
        ('2000', 'Accounts Payable',        'liability'),
        ('3000', 'Retained Earnings',       'equity'),
        ('4000', 'Sales Revenue',           'revenue'),
        ('5000', 'Cost of Goods Sold',      'expense'),
        ('5100', 'Payroll Expense',         'expense'),
        ('5200', 'Operating Expenses',      'expense'),
        ('5300', 'Employee Advances',       'asset'),
        ('5400', 'Damaged Goods / Wastage', 'expense'),
    ]
    accounts = {}
    for code, name, atype in defaults:
        accounts[code] = _get_or_create_account(code, name, atype)
    return accounts


def post_sale_journal(order, user=None):
    """
    When an order is placed:
      DR  Cash/Bank           (total_price)
      CR  Sales Revenue       (total_price)
    And for COGS:
      DR  COGS                (ingredient cost)
      CR  Inventory/Stock     (ingredient cost)
    """
    acc = _ensure_coa()

    revenue = order.total_price
    # Use the COGS value already snapshotted per line at the moment of sale
    # (OrderItem.cogs_at_sale, set in core.views.create_order using the
    # ingredient costs and stock actually deducted at that instant). Recomputing
    # from ingredients' CURRENT cost_per_unit here would silently diverge from
    # the real stock deduction whenever a restock changes ingredient cost, or a
    # recipe is edited, between the sale and this journal posting.
    cogs = order.items.aggregate(t=Sum('cogs_at_sale'))['t'] or Decimal('0')

    # Revenue entry
    je_rev = JournalEntry.objects.create(
        date=order.created_at.date(),
        description=f"Sale Order #{order.order_number}",
        source='sale', source_id=order.id,
        created_by=user,
    )
    JournalLine.objects.bulk_create([
        JournalLine(journal_entry=je_rev, account=acc['1000'], side='debit',  amount=revenue, description=f"Order {order.order_number}"),
        JournalLine(journal_entry=je_rev, account=acc['4000'], side='credit', amount=revenue, description=f"Order {order.order_number}"),
    ])

    if cogs > 0:
        je_cogs = JournalEntry.objects.create(
            date=order.created_at.date(),
            description=f"COGS Order #{order.order_number}",
            source='sale', source_id=order.id,
            created_by=user,
        )
        JournalLine.objects.bulk_create([
            JournalLine(journal_entry=je_cogs, account=acc['5000'], side='debit',  amount=cogs, description='COGS'),
            JournalLine(journal_entry=je_cogs, account=acc['1200'], side='credit', amount=cogs, description='Stock deduction'),
        ])


def post_purchase_journal(po, user=None):
    """
    When a PO is received:
      DR  Inventory/Stock     (total_amount)
      CR  Accounts Payable    (total_amount)
    """
    acc = _ensure_coa()
    amount = po.total_amount
    if amount <= 0:
        return
    je = JournalEntry.objects.create(
        date=po.received_date or po.order_date,
        description=f"Purchase {po.po_number} from {po.supplier.name}",
        source='purchase', source_id=po.id,
        created_by=user,
    )
    JournalLine.objects.bulk_create([
        JournalLine(journal_entry=je, account=acc['1200'], side='debit',  amount=amount, description=po.po_number),
        JournalLine(journal_entry=je, account=acc['2000'], side='credit', amount=amount, description=po.po_number),
    ])


def post_payroll_journal(salary_run, user=None):
    """
    When payroll is paid:
      DR  Payroll Expense     (total_gross)
      CR  Cash/Bank           (total_net)
      CR  Employee Advances   (total advances deducted)
    """
    acc = _ensure_coa()
    gross    = salary_run.total_gross
    advances = sum(l.advances for l in salary_run.lines.all())
    net      = salary_run.total_net
    if gross <= 0:
        return
    je = JournalEntry.objects.create(
        date=salary_run.run_date,
        description=f"Payroll {salary_run.period_start} → {salary_run.period_end}",
        source='payroll', source_id=salary_run.id,
        created_by=user,
    )
    lines = [
        JournalLine(journal_entry=je, account=acc['5100'], side='debit', amount=gross, description='Gross payroll'),
        JournalLine(journal_entry=je, account=acc['1000'], side='credit', amount=net,   description='Net salaries paid'),
    ]
    if advances > 0:
        lines.append(JournalLine(journal_entry=je, account=acc['5300'], side='credit', amount=advances, description='Advance deductions'))
    JournalLine.objects.bulk_create(lines)


def post_expense_journal(expense, user=None):
    """
    When an expense is recorded:
      DR  Operating Expenses  (amount)
      CR  Cash/Bank           (amount)
    """
    acc = _ensure_coa()
    je = JournalEntry.objects.create(
        date=expense.date,
        description=f"Expense: {expense.title}",
        source='expense', source_id=expense.id,
        created_by=user,
    )
    JournalLine.objects.bulk_create([
        JournalLine(journal_entry=je, account=acc['5200'], side='debit',  amount=expense.amount, description=expense.title),
        JournalLine(journal_entry=je, account=acc['1000'], side='credit', amount=expense.amount, description=expense.title),
    ])


def post_void_journal(order, user=None):
    """
    When an order is voided — reverse both the sale entry and the COGS entry,
    mirroring core.views.void_order, which physically restores the ingredient
    stock that was deducted at sale time.
      DR  Sales Revenue       (total_price)
      CR  Cash/Bank           (total_price)
      DR  Inventory/Stock     (cogs_at_sale total)
      CR  COGS                (cogs_at_sale total)
    """
    acc = _ensure_coa()
    amount = order.total_price
    cogs = order.items.aggregate(t=Sum('cogs_at_sale'))['t'] or Decimal('0')
    void_date = order.voided_at.date() if order.voided_at else order.created_at.date()

    if amount > 0:
        je = JournalEntry.objects.create(
            date=void_date,
            description=f"VOID Order #{order.order_number}",
            source='void', source_id=order.id,
            created_by=user,
        )
        JournalLine.objects.bulk_create([
            JournalLine(journal_entry=je, account=acc['4000'], side='debit',  amount=amount, description='Reversed sale'),
            JournalLine(journal_entry=je, account=acc['1000'], side='credit', amount=amount, description='Reversed cash'),
        ])

    if cogs > 0:
        # Without this entry, the Inventory/Stock account balance would never
        # reflect the ingredients void_order already restored to physical stock,
        # and COGS would stay permanently overstated for a sale that no longer happened.
        je_cogs = JournalEntry.objects.create(
            date=void_date,
            description=f"VOID COGS Order #{order.order_number}",
            source='void', source_id=order.id,
            created_by=user,
        )
        JournalLine.objects.bulk_create([
            JournalLine(journal_entry=je_cogs, account=acc['1200'], side='debit',  amount=cogs, description='Stock restored'),
            JournalLine(journal_entry=je_cogs, account=acc['5000'], side='credit', amount=cogs, description='Reversed COGS'),
        ])


def post_damage_journal(damage_records, order, user=None):
    """
    When ingredients are logged as damaged/wasted:

      DR  Damaged Goods / Wastage  5400  (total cost of damaged ingredients)
      CR  Inventory / Stock        1200  (remove the value from stock asset)

    This reflects:
    - The inventory asset decreasing (stock was physically lost)
    - The damage expense increasing (we absorbed the cost as a loss)

    damage_records: list of dicts  [{ingredient, qty_damaged, unit_cost}, ...]
    """
    acc = _ensure_coa()

    total_damage_cost = sum(
        Decimal(str(r['qty_damaged'])) * Decimal(str(r['unit_cost']))
        for r in damage_records
    )

    if total_damage_cost <= 0:
        return

    # Build line-by-line description
    detail_lines = ', '.join(
        f"{r['ingredient'].name} ×{float(r['qty_damaged']):.3f} {r['ingredient'].unit}"
        for r in damage_records
    )

    je = JournalEntry.objects.create(
        date=_get_today(),
        description=f"Damaged Goods — Order #{order.order_number} ({detail_lines[:180]})",
        source='damage',
        source_id=order.id,
        created_by=user,
    )

    JournalLine.objects.bulk_create([
        JournalLine(
            journal_entry=je,
            account=acc['5400'],
            side='debit',
            amount=total_damage_cost,
            description=f"Wastage loss — {detail_lines[:180]}",
        ),
        JournalLine(
            journal_entry=je,
            account=acc['1200'],
            side='credit',
            amount=total_damage_cost,
            description=f"Inventory reduction — damaged goods",
        ),
    ])

    return je