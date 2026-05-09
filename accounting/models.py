from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone


class Account(models.Model):
    TYPE_CHOICES = [
        ('asset',     'Asset'),
        ('liability', 'Liability'),
        ('equity',    'Equity'),
        ('revenue',   'Revenue'),
        ('expense',   'Expense'),
    ]
    code        = models.CharField(max_length=10, unique=True)  # e.g. 1010
    name        = models.CharField(max_length=150)
    account_type = models.CharField(max_length=12, choices=TYPE_CHOICES)
    parent      = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name='children')
    description = models.TextField(blank=True)
    is_active   = models.BooleanField(default=True)
    is_system   = models.BooleanField(default=False)  # protected system accounts

    class Meta:
        ordering = ['code']

    def __str__(self):
        return f"{self.code} — {self.name}"

    @property
    def balance(self):
        debits  = self.journal_lines.filter(side='debit').aggregate(
            t=models.Sum('amount'))['t'] or 0
        credits = self.journal_lines.filter(side='credit').aggregate(
            t=models.Sum('amount'))['t'] or 0
        # Asset/Expense: debit-normal; Liability/Equity/Revenue: credit-normal
        if self.account_type in ('asset', 'expense'):
            return debits - credits
        return credits - debits


class JournalEntry(models.Model):
    SOURCE_CHOICES = [
        ('sale',      'Sale'),
        ('purchase',  'Purchase / Restock'),
        ('payroll',   'Payroll'),
        ('expense',   'Expense'),
        ('manual',    'Manual'),
        ('void',      'Order Void'),
        ('advance',   'Employee Advance'),
        ('damage',    'Damaged Goods / Wastage'),
    ]
    entry_number = models.CharField(max_length=20, unique=True, editable=False)
    date         = models.DateField(default=timezone.now)
    description  = models.CharField(max_length=255)
    source       = models.CharField(max_length=12, choices=SOURCE_CHOICES, default='manual')
    source_id    = models.PositiveIntegerField(null=True, blank=True)   # FK to originating record
    is_posted    = models.BooleanField(default=True)
    created_by   = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date', '-created_at']

    def save(self, *args, **kwargs):
        if not self.entry_number:
            import uuid
            self.entry_number = 'JE-' + uuid.uuid4().hex[:8].upper()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.entry_number} — {self.description}"

    def is_balanced(self):
        from django.db.models import Sum
        lines = self.lines.aggregate(
            dr=Sum('amount', filter=models.Q(side='debit')),
            cr=Sum('amount', filter=models.Q(side='credit')),
        )
        return (lines['dr'] or 0) == (lines['cr'] or 0)


class JournalLine(models.Model):
    SIDE_CHOICES = [('debit', 'Debit'), ('credit', 'Credit')]
    journal_entry = models.ForeignKey(JournalEntry, on_delete=models.CASCADE, related_name='lines')
    account       = models.ForeignKey(Account, on_delete=models.PROTECT, related_name='journal_lines')
    side          = models.CharField(max_length=6, choices=SIDE_CHOICES)
    amount        = models.DecimalField(max_digits=14, decimal_places=2)
    description   = models.CharField(max_length=200, blank=True)

    def __str__(self):
        return f"{self.get_side_display()} {self.amount} → {self.account}"
