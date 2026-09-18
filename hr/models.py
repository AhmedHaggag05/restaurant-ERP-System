from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from core.models import Employee


class AttendanceRecord(models.Model):
    STATUS_CHOICES = [
        ('present',  'Present'),
        ('absent',   'Absent'),
        ('late',     'Late'),
        ('half_day', 'Half Day'),
        ('leave',    'On Leave'),
    ]
    employee   = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='attendance')
    date       = models.DateField(default=timezone.now)
    status     = models.CharField(max_length=10, choices=STATUS_CHOICES, default='present')
    check_in   = models.TimeField(null=True, blank=True)
    check_out  = models.TimeField(null=True, blank=True)
    notes      = models.CharField(max_length=200, blank=True)
    recorded_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('employee', 'date')
        ordering = ['-date']

    def __str__(self):
        return f"{self.employee.full_name} — {self.date} — {self.get_status_display()}"


class SalaryRun(models.Model):
    STATUS_CHOICES = [
        ('draft',  'Draft'),
        ('paid',   'Paid'),
    ]
    period_start = models.DateField()
    period_end   = models.DateField()
    run_date     = models.DateField(default=timezone.now)
    status       = models.CharField(max_length=10, choices=STATUS_CHOICES, default='draft')
    total_gross  = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_deductions = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_net    = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    notes        = models.TextField(blank=True)
    created_by   = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-period_end']

    def __str__(self):
        return f"Salary Run {self.period_start} → {self.period_end} ({self.get_status_display()})"


class SalaryLine(models.Model):
    salary_run  = models.ForeignKey(SalaryRun, on_delete=models.CASCADE, related_name='lines')
    employee    = models.ForeignKey(Employee, on_delete=models.CASCADE)
    base_salary = models.DecimalField(max_digits=10, decimal_places=2)
    days_worked = models.DecimalField(max_digits=5, decimal_places=1, default=0)
    days_total  = models.DecimalField(max_digits=5, decimal_places=1, default=30)
    gross       = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    deductions  = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    advances    = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    net         = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    notes       = models.CharField(max_length=200, blank=True)

    def __str__(self):
        return f"{self.employee.full_name} — {self.salary_run}"


class Advance(models.Model):
    STATUS_CHOICES = [
        ('pending',  'Pending'),
        ('approved', 'Approved'),
        ('paid',     'Paid'),
        ('rejected', 'Rejected'),
        ('deducted', 'Deducted'),
    ]
    employee   = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='advances')
    amount     = models.DecimalField(max_digits=10, decimal_places=2)
    reason     = models.CharField(max_length=200, blank=True)
    date       = models.DateField(default=timezone.now)
    status     = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    approved_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name='approved_advances')
    paid_at = models.DateTimeField(null=True, blank=True)
    paid_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                related_name='paid_advances')
    deducted_in = models.ForeignKey(SalaryRun, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name='deducted_advances')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date']

    def __str__(self):
        return f"{self.employee.full_name} — {self.amount} EGP ({self.get_status_display()})"
