from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from core.models import Ingredient


class Supplier(models.Model):
    name        = models.CharField(max_length=150, unique=True)
    contact     = models.CharField(max_length=100, blank=True)
    phone       = models.CharField(max_length=30, blank=True)
    email       = models.EmailField(blank=True)
    address     = models.TextField(blank=True)
    notes       = models.TextField(blank=True)
    is_active   = models.BooleanField(default=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class PurchaseOrder(models.Model):
    STATUS_CHOICES = [
        ('draft',     'Draft'),
        ('sent',      'Sent to Supplier'),
        ('received',  'Received'),
        ('partial',   'Partially Received'),
        ('cancelled', 'Cancelled'),
    ]
    po_number   = models.CharField(max_length=20, unique=True, editable=False)
    supplier    = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name='purchase_orders')
    status      = models.CharField(max_length=12, choices=STATUS_CHOICES, default='draft')
    order_date  = models.DateField(default=timezone.now)
    expected_date = models.DateField(null=True, blank=True)
    received_date = models.DateField(null=True, blank=True)
    notes       = models.TextField(blank=True)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    created_by  = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name='purchase_orders')
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        if not self.po_number:
            import uuid
            self.po_number = 'PO-' + uuid.uuid4().hex[:6].upper()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.po_number} — {self.supplier.name}"


class PurchaseOrderLine(models.Model):
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name='lines')
    ingredient     = models.ForeignKey(Ingredient, null=True, blank=True, on_delete=models.SET_NULL)
    quantity_ordered  = models.DecimalField(max_digits=12, decimal_places=3)
    quantity_received = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    unit_cost      = models.DecimalField(max_digits=12, decimal_places=4)
    total_cost     = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    notes          = models.CharField(max_length=200, blank=True)

    def save(self, *args, **kwargs):
        self.total_cost = self.quantity_ordered * self.unit_cost
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.ingredient.name} × {self.quantity_ordered} ({self.purchase_order.po_number})"


class SupplierInvoice(models.Model):
    STATUS_CHOICES = [
        ('unpaid', 'Unpaid'),
        ('paid',   'Paid'),
        ('partial','Partially Paid'),
    ]
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE,
                                       related_name='invoices', null=True, blank=True)
    supplier      = models.ForeignKey(Supplier, on_delete=models.PROTECT)
    invoice_number = models.CharField(max_length=60, blank=True)
    invoice_date  = models.DateField(default=timezone.now)
    due_date      = models.DateField(null=True, blank=True)
    amount        = models.DecimalField(max_digits=12, decimal_places=2)
    amount_paid   = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    status        = models.CharField(max_length=10, choices=STATUS_CHOICES, default='unpaid')
    notes         = models.TextField(blank=True)
    created_by    = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    created_at    = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-invoice_date']

    def __str__(self):
        return f"INV {self.invoice_number or self.id} — {self.supplier.name} — {self.amount} EGP"
