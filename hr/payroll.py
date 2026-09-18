"""Atomic payroll state transitions and accounting validation."""

from decimal import Decimal

from django.db import transaction
from django.db.models import Sum

from accounting.utils import post_payroll_journal
from .models import Advance, SalaryRun


@transaction.atomic
def mark_salary_run_paid(run_id, user):
    run = SalaryRun.objects.select_for_update().get(pk=run_id)
    if run.status != 'draft':
        raise ValueError('This salary run is already paid.')
    lines = list(run.lines.select_related('employee').all())
    for line in lines:
        reserved = (Advance.objects.filter(deducted_in=run, employee=line.employee,
                                           status='approved')
                    .aggregate(total=Sum('amount'))['total'] or Decimal('0'))
        if (min(line.gross, line.deductions, line.advances, line.net) < 0 or
                line.advances != reserved or
                line.net != line.gross - line.deductions - line.advances):
            raise ValueError(f'Payroll amounts for {line.employee.full_name} need correction before payment.')
    if (sum((line.gross for line in lines), Decimal('0')) != run.total_gross or
            sum((line.deductions for line in lines), Decimal('0')) != run.total_deductions or
            sum((line.net for line in lines), Decimal('0')) != run.total_net):
        raise ValueError('Payroll totals need recalculation before payment.')

    run.status = 'paid'
    run.save(update_fields=['status'])
    Advance.objects.filter(deducted_in=run, status='approved').update(status='deducted')
    post_payroll_journal(run, user)
    return run
