from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.db.models import Sum, Count, Q
from decimal import Decimal
from datetime import date, timedelta

from core.models import Employee
from core.views import manager_required
from .models import AttendanceRecord, SalaryRun, SalaryLine, Advance


# ── Employees (redirect to core page but from ERP layout) ─────────────────

@manager_required
def employees(request):
    emps = Employee.objects.annotate(
        pending_advances=Count('advances', filter=Q(advances__status='pending'))
    ).order_by('full_name')
    return render(request, 'hr/employees.html', {'employees': emps})


# ── Attendance ────────────────────────────────────────────────────────────

@manager_required
def attendance(request):
    today = date.today()
    sel_date_str = request.GET.get('date', today.isoformat())
    try:
        sel_date = date.fromisoformat(sel_date_str)
    except ValueError:
        sel_date = today

    if request.method == 'POST':
        emp_id = request.POST.get('employee_id')
        emp = get_object_or_404(Employee, id=emp_id)
        rec_date_str = request.POST.get('date', today.isoformat())
        try:
            rec_date = date.fromisoformat(rec_date_str)
        except ValueError:
            rec_date = today
        status = request.POST.get('status', 'present')
        check_in  = request.POST.get('check_in') or None
        check_out = request.POST.get('check_out') or None
        notes     = request.POST.get('notes', '').strip()

        # Normalize time values — Django template may render TimeField as
        # "9:07 a.m." format; we need "HH:MM" for the model.
        def normalize_time(val):
            if not val:
                return None
            val = val.strip()
            # Already HH:MM or HH:MM:SS → pass through
            if len(val) <= 8 and ':' in val and 'a' not in val.lower() and 'p' not in val.lower():
                return val[:5] if len(val) >= 5 else val
            # Parse Django's "9:07 a.m." / "9:07 p.m." format
            try:
                import re
                m = re.match(r'^(\d{1,2}):(\d{2})(?::\d+)?\s*(a\.?m\.?|p\.?m\.?)?$', val, re.IGNORECASE)
                if m:
                    h, mn, ampm = int(m.group(1)), int(m.group(2)), (m.group(3) or '').lower().replace('.','')
                    if ampm == 'pm' and h != 12:
                        h += 12
                    elif ampm == 'am' and h == 12:
                        h = 0
                    return f'{h:02d}:{mn:02d}'
            except Exception:
                pass
            return None  # unparseable → drop it

        check_in  = normalize_time(check_in)
        check_out = normalize_time(check_out)
        AttendanceRecord.objects.update_or_create(
            employee=emp, date=rec_date,
            defaults=dict(status=status, check_in=check_in,
                          check_out=check_out, notes=notes,
                          recorded_by=request.user)
        )
        messages.success(request, f"Attendance recorded for {emp.full_name}.")
        return redirect(f"{request.path}?date={rec_date_str}")

    employees = Employee.objects.filter(is_active=True).order_by('full_name')
    records   = {r.employee_id: r for r in
                 AttendanceRecord.objects.filter(date=sel_date).select_related('employee')}

    # Summary for selected date
    total = employees.count()
    present = sum(1 for r in records.values() if r.status in ('present', 'late'))
    absent  = total - len(records)

    # Month attendance summary per employee
    month_start = sel_date.replace(day=1)
    monthly = AttendanceRecord.objects.filter(
        date__gte=month_start, date__lte=sel_date
    ).values('employee_id', 'status').annotate(cnt=Count('id'))

    return render(request, 'hr/attendance.html', {
        'employees': employees, 'records': records,
        'sel_date': sel_date, 'today': today,
        'total': total, 'present': present, 'absent': absent,
        'STATUS_CHOICES': AttendanceRecord.STATUS_CHOICES,
    })


# ── Payroll / Salary Runs ─────────────────────────────────────────────────

@manager_required
def salary_runs(request):
    runs = SalaryRun.objects.prefetch_related('lines').all()
    return render(request, 'hr/salary_runs.html', {'runs': runs})


@manager_required
def create_salary_run(request):
    if request.method == 'POST':
        period_start = request.POST.get('period_start')
        period_end   = request.POST.get('period_end')
        notes        = request.POST.get('notes', '').strip()

        try:
            ps = date.fromisoformat(period_start)
            pe = date.fromisoformat(period_end)
        except (ValueError, TypeError):
            messages.error(request, "Invalid dates.")
            return redirect('hr:salary_runs')

        run = SalaryRun.objects.create(
            period_start=ps, period_end=pe, notes=notes,
            created_by=request.user
        )

        # Auto-generate lines for all active employees
        employees = Employee.objects.filter(is_active=True)
        days_in_period = (pe - ps).days + 1

        for emp in employees:
            # Count attendance in this period
            att = AttendanceRecord.objects.filter(
                employee=emp, date__range=[ps, pe]
            )
            days_present = att.filter(status__in=['present', 'late']).count()
            days_half    = att.filter(status='half_day').count()
            days_worked  = Decimal(days_present) + Decimal(days_half) * Decimal('0.5')

            # Outstanding approved advances
            advance_total = emp.advances.filter(status='approved').aggregate(
                t=Sum('amount'))['t'] or Decimal('0')

            gross = emp.salary  # full monthly salary; prorate if needed
            # Simple proration: salary * days_worked / days_in_period
            if days_in_period > 0 and days_worked > 0:
                gross = (emp.salary * days_worked / days_in_period).quantize(Decimal('0.01'))

            net = max(Decimal('0'), gross - advance_total)

            SalaryLine.objects.create(
                salary_run=run, employee=emp,
                base_salary=emp.salary,
                days_worked=days_worked,
                days_total=Decimal(days_in_period),
                gross=gross,
                deductions=Decimal('0'),
                advances=advance_total,
                net=net,
            )

        # Update run totals
        run.total_gross      = run.lines.aggregate(t=Sum('gross'))['t'] or 0
        run.total_deductions = run.lines.aggregate(t=Sum('deductions'))['t'] or 0
        run.total_net        = run.lines.aggregate(t=Sum('net'))['t'] or 0
        run.save()

        messages.success(request, f"Salary run created for {ps} → {pe}.")
        return redirect('hr:salary_run_detail', pk=run.pk)

    return render(request, 'hr/create_salary_run.html')


@manager_required
def salary_run_detail(request, pk):
    run = get_object_or_404(SalaryRun, pk=pk)
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'mark_paid':
            run.status = 'paid'
            run.save()
            # Mark all deducted advances
            for line in run.lines.all():
                if line.advances > 0:
                    line.employee.advances.filter(status='approved').update(
                        status='deducted', deducted_in=run)
            # Create accounting journal entry
            try:
                from accounting.utils import post_payroll_journal
                post_payroll_journal(run, request.user)
            except Exception:
                pass
            messages.success(request, "Salary run marked as paid.")
        elif action == 'edit_line':
            line_id = request.POST.get('line_id')
            line = get_object_or_404(SalaryLine, id=line_id, salary_run=run)
            line.gross      = Decimal(request.POST.get('gross', str(line.gross)) or '0')
            line.deductions = Decimal(request.POST.get('deductions', '0') or '0')
            line.advances   = Decimal(request.POST.get('advances', '0') or '0')
            line.notes      = request.POST.get('notes', '').strip()
            line.net        = max(Decimal('0'), line.gross - line.deductions - line.advances)
            line.save()
            run.total_gross      = run.lines.aggregate(t=Sum('gross'))['t'] or 0
            run.total_deductions = run.lines.aggregate(t=Sum('deductions'))['t'] or 0
            run.total_net        = run.lines.aggregate(t=Sum('net'))['t'] or 0
            run.save()
            messages.success(request, "Line updated.")
        return redirect('hr:salary_run_detail', pk=pk)

    lines = run.lines.select_related('employee').all()
    return render(request, 'hr/salary_run_detail.html', {'run': run, 'lines': lines})


# ── Advances ──────────────────────────────────────────────────────────────

@manager_required
def advances(request):
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'add':
            emp_id = request.POST.get('employee_id')
            emp    = get_object_or_404(Employee, id=emp_id)
            amount = Decimal(request.POST.get('amount', '0') or '0')
            reason = request.POST.get('reason', '').strip()
            adv_date = request.POST.get('date', date.today().isoformat())
            Advance.objects.create(employee=emp, amount=amount, reason=reason, date=adv_date)
            messages.success(request, f"Advance of {amount} EGP recorded for {emp.full_name}.")
        elif action == 'approve':
            adv = get_object_or_404(Advance, id=request.POST.get('advance_id'))
            adv.status = 'approved'
            adv.approved_by = request.user
            adv.save()
            messages.success(request, "Advance approved.")
        elif action == 'reject':
            adv = get_object_or_404(Advance, id=request.POST.get('advance_id'))
            adv.status = 'rejected'
            adv.save()
            messages.success(request, "Advance rejected.")
        return redirect('hr:advances')

    all_advances = Advance.objects.select_related('employee', 'approved_by').all()
    employees = Employee.objects.filter(is_active=True).order_by('full_name')
    pending_total = all_advances.filter(status='approved').aggregate(t=Sum('amount'))['t'] or 0
    return render(request, 'hr/advances.html', {
        'advances': all_advances, 'employees': employees,
        'pending_total': pending_total,
    })
