from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.db.models import Sum, Q
from decimal import Decimal
from datetime import date

from core.views import manager_required
from .models import Account, JournalEntry, JournalLine
from .utils import _ensure_coa


# ── Chart of Accounts ─────────────────────────────────────────────────────

@manager_required
def chart_of_accounts(request):
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'add':
            code = request.POST.get('code', '').strip()
            name = request.POST.get('name', '').strip()
            atype = request.POST.get('account_type', 'expense')
            parent_id = request.POST.get('parent_id') or None
            if not code or not name:
                messages.error(request, "Code and name are required.")
            elif Account.objects.filter(code=code).exists():
                messages.error(request, f"Account code '{code}' already exists.")
            else:
                parent = Account.objects.filter(id=parent_id).first() if parent_id else None
                Account.objects.create(code=code, name=name, account_type=atype, parent=parent,
                                       description=request.POST.get('description', '').strip())
                messages.success(request, f"Account {code} — {name} added.")
        elif action == 'edit':
            acc = get_object_or_404(Account, id=request.POST.get('account_id'))
            if not acc.is_system:
                acc.name        = request.POST.get('name', acc.name).strip()
                acc.account_type = request.POST.get('account_type', acc.account_type)
                acc.description  = request.POST.get('description', '').strip()
                acc.is_active    = request.POST.get('is_active') == 'on'
                acc.save()
                messages.success(request, f"Account {acc.code} updated.")
            else:
                messages.warning(request, "System accounts cannot be edited.")
        elif action == 'seed':
            _ensure_coa()
            messages.success(request, "Default chart of accounts created.")
        return redirect('accounting:chart_of_accounts')

    accounts = Account.objects.all()
    by_type = {}
    for acc in accounts:
        by_type.setdefault(acc.account_type, []).append(acc)
    type_order = ['asset', 'liability', 'equity', 'revenue', 'expense']
    type_labels = dict(Account.TYPE_CHOICES)
    return render(request, 'accounting/chart_of_accounts.html', {
        'accounts': accounts, 'by_type': by_type,
        'type_order': type_order, 'type_labels': type_labels,
        'TYPE_CHOICES': Account.TYPE_CHOICES,
    })


# ── Journal ───────────────────────────────────────────────────────────────

@manager_required
def journal(request):
    if request.method == 'POST':
        # Manual journal entry
        desc     = request.POST.get('description', '').strip()
        je_date  = request.POST.get('date', date.today().isoformat())
        dr_account_id = request.POST.get('dr_account')
        cr_account_id = request.POST.get('cr_account')
        amount   = Decimal(request.POST.get('amount', '0') or '0')
        if not desc or amount <= 0:
            messages.error(request, "Description and amount are required.")
        elif dr_account_id == cr_account_id:
            messages.error(request, "Debit and credit accounts must differ.")
        else:
            dr_acc = get_object_or_404(Account, id=dr_account_id)
            cr_acc = get_object_or_404(Account, id=cr_account_id)
            je = JournalEntry.objects.create(
                date=je_date, description=desc, source='manual', created_by=request.user
            )
            JournalLine.objects.bulk_create([
                JournalLine(journal_entry=je, account=dr_acc, side='debit',  amount=amount),
                JournalLine(journal_entry=je, account=cr_acc, side='credit', amount=amount),
            ])
            messages.success(request, f"Journal entry {je.entry_number} posted.")
        return redirect('accounting:journal')

    # Filters
    source_filter = request.GET.get('source', '')
    date_from = request.GET.get('date_from', '')
    date_to   = request.GET.get('date_to', '')
    qs = JournalEntry.objects.prefetch_related('lines__account').all()
    if source_filter:
        qs = qs.filter(source=source_filter)
    if date_from:
        qs = qs.filter(date__gte=date_from)
    if date_to:
        qs = qs.filter(date__lte=date_to)
    qs = qs[:500]

    accounts = Account.objects.filter(is_active=True).order_by('code')
    return render(request, 'accounting/journal.html', {
        'entries': qs, 'accounts': accounts,
        'source_filter': source_filter, 'date_from': date_from, 'date_to': date_to,
        'SOURCE_CHOICES': JournalEntry.SOURCE_CHOICES,
    })


# ── P&L Report ────────────────────────────────────────────────────────────

@manager_required
def profit_loss(request):
    today = date.today()
    date_from = request.GET.get('date_from', today.replace(day=1).isoformat())
    date_to   = request.GET.get('date_to',   today.isoformat())

    def account_balance_in_range(account_type, date_from, date_to):
        lines = JournalLine.objects.filter(
            account__account_type=account_type,
            account__is_active=True,
            journal_entry__date__gte=date_from,
            journal_entry__date__lte=date_to,
            journal_entry__is_posted=True,
        )
        if account_type in ('asset', 'expense'):
            dr = lines.filter(side='debit').aggregate(t=Sum('amount'))['t'] or 0
            cr = lines.filter(side='credit').aggregate(t=Sum('amount'))['t'] or 0
            return dr - cr
        else:
            cr = lines.filter(side='credit').aggregate(t=Sum('amount'))['t'] or 0
            dr = lines.filter(side='debit').aggregate(t=Sum('amount'))['t'] or 0
            return cr - dr

    # Revenue breakdown by account
    rev_accounts = Account.objects.filter(account_type='revenue', is_active=True)
    revenue_lines = []
    total_revenue = Decimal('0')
    for acc in rev_accounts:
        bal = _account_balance_range(acc, date_from, date_to)
        if bal != 0:
            revenue_lines.append({'account': acc, 'amount': bal})
            total_revenue += bal

    # Expense breakdown by account
    exp_accounts = Account.objects.filter(account_type='expense', is_active=True)
    expense_lines = []
    total_expenses = Decimal('0')
    for acc in exp_accounts:
        bal = _account_balance_range(acc, date_from, date_to)
        if bal != 0:
            expense_lines.append({'account': acc, 'amount': bal})
            total_expenses += bal

    net_profit = total_revenue - total_expenses

    return render(request, 'accounting/profit_loss.html', {
        'date_from': date_from, 'date_to': date_to,
        'revenue_lines': revenue_lines, 'total_revenue': total_revenue,
        'expense_lines': expense_lines, 'total_expenses': total_expenses,
        'net_profit': net_profit,
    })


def _account_balance_range(account, date_from, date_to):
    lines = JournalLine.objects.filter(
        account=account,
        journal_entry__date__gte=date_from,
        journal_entry__date__lte=date_to,
        journal_entry__is_posted=True,
    )
    dr = lines.filter(side='debit').aggregate(t=Sum('amount'))['t'] or Decimal('0')
    cr = lines.filter(side='credit').aggregate(t=Sum('amount'))['t'] or Decimal('0')
    if account.account_type in ('asset', 'expense'):
        return dr - cr
    return cr - dr


# ── Balance Sheet ─────────────────────────────────────────────────────────

@manager_required
def balance_sheet(request):
    today = date.today()
    as_of = request.GET.get('as_of', today.isoformat())

    def get_balance_as_of(account, as_of_date):
        lines = JournalLine.objects.filter(
            account=account,
            journal_entry__date__lte=as_of_date,
            journal_entry__is_posted=True,
        )
        dr = lines.filter(side='debit').aggregate(t=Sum('amount'))['t'] or Decimal('0')
        cr = lines.filter(side='credit').aggregate(t=Sum('amount'))['t'] or Decimal('0')
        if account.account_type in ('asset', 'expense'):
            return dr - cr
        return cr - dr

    sections = {}
    for atype in ['asset', 'liability', 'equity']:
        accs = Account.objects.filter(account_type=atype, is_active=True)
        lines = []
        total = Decimal('0')
        for acc in accs:
            bal = get_balance_as_of(acc, as_of)
            if bal != 0:
                lines.append({'account': acc, 'amount': bal})
                total += bal
        sections[atype] = {'lines': lines, 'total': total}

    total_assets = sections['asset']['total']
    total_liab   = sections['liability']['total']
    total_equity = sections['equity']['total']
    balanced     = abs(total_assets - (total_liab + total_equity)) < Decimal('0.01')

    return render(request, 'accounting/balance_sheet.html', {
        'as_of': as_of, 'sections': sections,
        'total_assets': total_assets,
        'total_liab': total_liab,
        'total_equity': total_equity,
        'balanced': balanced,
    })
