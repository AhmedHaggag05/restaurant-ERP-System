from django.db import models
from django.db import transaction
from django.utils import timezone
from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver
import uuid

class UserProfile(models.Model):
    ROLE_CHOICES = [
        ('superadmin', 'Super Admin'),
        ('manager',    'Manager'),
        ('cashier',    'Cashier'),
    ]
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    role = models.CharField(max_length=12, choices=ROLE_CHOICES, default='cashier')

    def __str__(self):
        return f"{self.user.username} - {self.get_role_display()}"

class Ingredient(models.Model):
    UNIT_CHOICES = [
        ('kg',     'Kilogram (kg)'),
        ('L',      'Liter (L)'),
        ('pieces', 'Pieces'),
        ('boxes',  'Boxes'),
    ]
    SUB_UNIT_CHOICES = [
        ('cans',    'Cans'),
        ('packets', 'Packets'),
    ]
    name = models.CharField(max_length=100, unique=True)
    stock_quantity = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    unit = models.CharField(max_length=20, default='kg', choices=UNIT_CHOICES)
    cost_per_unit = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    low_stock_threshold = models.DecimalField(max_digits=10, decimal_places=3, default=10)
    # Box breakdown (only relevant when unit='boxes')
    units_per_box = models.PositiveIntegerField(default=1,
        help_text='How many cans/packets per box')
    sub_unit = models.CharField(max_length=10, choices=SUB_UNIT_CHOICES, default='packets',
        help_text='What each box contains')
    is_active = models.BooleanField(default=True,
        help_text='Inactive ingredients are hidden from POS and new recipes but history is preserved')

    @property
    def display_stock(self):
        """Human-readable stock e.g. '180 packets (6 boxes × 30 packets/box)'"""
        if self.unit == 'boxes':
            boxes = self.stock_quantity / self.units_per_box if self.units_per_box else 0
            return f"{self.stock_quantity:.0f} {self.sub_unit} ({boxes:.1f} boxes × {self.units_per_box} {self.sub_unit}/box)"
        return f"{self.stock_quantity} {self.unit}"

    def __str__(self):
        return f"{self.name} ({self.display_stock})"

class MenuItem(models.Model):
    name        = models.CharField(max_length=100, unique=True)
    price       = models.DecimalField(max_digits=10, decimal_places=2)
    description = models.TextField(blank=True)
    image_url   = models.URLField(blank=True, null=True)
    is_active   = models.BooleanField(default=True)
    category    = models.ForeignKey('MenuCategory', null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name='items')

    def __str__(self):
        return self.name

class MenuItemIngredient(models.Model):
    RECIPE_UNIT_CHOICES = [
        ('g',       'grams (g)'),
        ('mL',      'milliliters (mL)'),
        ('packets', 'packets'),
        ('cans',    'cans'),
        ('pieces',  'pieces'),
    ]
    menu_item = models.ForeignKey(MenuItem, related_name='ingredients', on_delete=models.CASCADE)
    ingredient = models.ForeignKey(Ingredient, on_delete=models.CASCADE)
    quantity_required = models.DecimalField(max_digits=10, decimal_places=3)
    recipe_unit = models.CharField(max_length=10, choices=RECIPE_UNIT_CHOICES, default='g')

    def __str__(self):
        return f"{self.quantity_required} {self.recipe_unit} of {self.ingredient.name} for {self.menu_item.name}"

class PromoCode(models.Model):
    code         = models.CharField(max_length=30, unique=True)
    description  = models.CharField(max_length=100, blank=True)
    discount_type = models.CharField(max_length=10, choices=[('percent','Percent %'),('fixed','Fixed EGP')], default='percent')
    discount_value = models.DecimalField(max_digits=8, decimal_places=2)
    valid_from   = models.DateField(null=True, blank=True, help_text='Start date (leave blank = always valid from start)')
    valid_until  = models.DateField(null=True, blank=True, help_text='Expiry date (leave blank = never expires)')
    max_uses     = models.PositiveIntegerField(null=True, blank=True, help_text='Max total uses (blank = unlimited)')
    times_used   = models.PositiveIntegerField(default=0)
    is_active    = models.BooleanField(default=True)
    created_at   = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"{self.code} ({self.discount_type}: {self.discount_value})"


class Expense(models.Model):
    CATEGORY_CHOICES = [
        ('rent',       'Rent'),
        ('utilities',  'Utilities'),
        ('packaging',  'Packaging'),
        ('maintenance','Maintenance'),
        ('marketing',  'Marketing'),
        ('other',      'Other'),
    ]
    title       = models.CharField(max_length=150)
    category    = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default='other')
    amount      = models.DecimalField(max_digits=10, decimal_places=2)
    date        = models.DateField(default=timezone.now)
    notes       = models.TextField(blank=True)
    is_voided   = models.BooleanField(default=False)
    voided_at   = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.title} — {self.amount} EGP"


class Order(models.Model):
    STATUS_CHOICES = [
        ('active',  'Active'),
        ('voided',  'Voided'),
    ]
    order_number  = models.CharField(max_length=12, unique=True, editable=False)
    created_at    = models.DateTimeField(default=timezone.now)
    total_price   = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    customer_notes= models.TextField(blank=True)
    table_number  = models.CharField(max_length=10, blank=True)
    promo_code    = models.ForeignKey('PromoCode', null=True, blank=True, on_delete=models.SET_NULL)
    discount_amount = models.DecimalField(max_digits=8, decimal_places=2, default=0.00)
    status        = models.CharField(max_length=10, choices=STATUS_CHOICES, default='active')
    voided_at     = models.DateTimeField(null=True, blank=True)
    voided_by     = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='voided_orders')
    void_reason   = models.CharField(max_length=200, blank=True)
    
    def save(self, *args, **kwargs):
        if not self.order_number:
            self.order_number = str(uuid.uuid4().hex[:8]).upper()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Order {self.order_number}"

def _cogs_column_exists():
    """Check once whether cogs_at_sale column exists in the DB."""
    try:
        from django.db import connection
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() "
                "AND TABLE_NAME = 'core_orderitem' "
                "AND COLUMN_NAME = 'cogs_at_sale'"
            )
            return cursor.fetchone()[0] > 0
    except Exception:
        return False


class _SafeOrderItemManager(models.Manager):
    """Defers cogs_at_sale when the column doesn't exist yet (pre-migration)."""
    _col_exists = None  # cached after first check

    def get_queryset(self):
        qs = super().get_queryset()
        if _SafeOrderItemManager._col_exists is None:
            _SafeOrderItemManager._col_exists = _cogs_column_exists()
        if not _SafeOrderItemManager._col_exists:
            qs = qs.defer('cogs_at_sale')
        return qs


class OrderItem(models.Model):
    order = models.ForeignKey(Order, related_name='items', on_delete=models.CASCADE)
    menu_item = models.ForeignKey(MenuItem, on_delete=models.SET_NULL, null=True)
    quantity = models.PositiveIntegerField(default=1)
    price_at_order = models.DecimalField(max_digits=10, decimal_places=2)
    # COGS snapshotted at sale time — total cost for this line (qty × unit_cogs)
    # Ensures historical reports stay accurate even after ingredient cost changes
    cogs_at_sale = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    # Existing rows predate ingredient snapshots and must not be quantity-edited.
    stock_snapshot_ready = models.BooleanField(default=False)

    objects = _SafeOrderItemManager()

    class Meta:
        base_manager_name = 'objects'

    def __str__(self):
        return f"{self.quantity} x {self.menu_item.name}"


class OrderItemConsumption(models.Model):
    """Actual stock consumed by an order line, independent of later recipe edits."""
    order_item = models.ForeignKey(OrderItem, related_name='consumptions', on_delete=models.CASCADE)
    ingredient = models.ForeignKey(Ingredient, on_delete=models.PROTECT)
    quantity_consumed = models.DecimalField(max_digits=12, decimal_places=3)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=['order_item', 'ingredient'], name='unique_order_item_ingredient_consumption')]


class Employee(models.Model):
    JOB_CHOICES = [
        ('manager',       'Manager'),
        ('cashier',       'Cashier'),
        ('chef',          'Chef'),
        ('waiter',        'Waiter'),
        ('cleaner',       'Cleaner'),
        ('security',      'Security'),
        ('delivery',      'Delivery'),
        ('other',         'Other'),
    ]
    full_name    = models.CharField(max_length=150)
    phone        = models.CharField(max_length=30, blank=True)
    job_title    = models.CharField(max_length=20, choices=JOB_CHOICES, default='cashier')
    salary       = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    hire_date    = models.DateField(default=timezone.now)
    is_active    = models.BooleanField(default=True)
    notes        = models.TextField(blank=True)

    def __str__(self):
        return f"{self.full_name} ({self.get_job_title_display()})"



# ── Menu Category ─────────────────────────────────────────────────────────────
class MenuCategory(models.Model):
    name     = models.CharField(max_length=60, unique=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['sort_order', 'name']

    def __str__(self):
        return self.name


# ── Stock Adjustment Log ──────────────────────────────────────────────────────
class StockLog(models.Model):
    REASON_CHOICES = [
        ('sale',     'Sale'),
        ('restock',  'Restock'),
        ('manual',   'Manual Adjustment'),
        ('void',     'Order Void'),
        ('damaged',  'Damaged / Wastage'),
        ('edit',     'Order Edit'),
    ]
    ingredient   = models.ForeignKey(Ingredient, on_delete=models.CASCADE, related_name='stock_logs')
    changed_by   = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    reason       = models.CharField(max_length=12, choices=REASON_CHOICES)
    qty_before   = models.DecimalField(max_digits=12, decimal_places=3)
    qty_change   = models.DecimalField(max_digits=12, decimal_places=3)  # negative = deduction
    qty_after    = models.DecimalField(max_digits=12, decimal_places=3)
    note         = models.CharField(max_length=200, blank=True)
    created_at   = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.ingredient.name} {self.qty_change:+.3f} ({self.reason})"


# ── Ingredient Cost History ───────────────────────────────────────────────────
class CostHistory(models.Model):
    ingredient    = models.ForeignKey(Ingredient, on_delete=models.CASCADE, related_name='cost_history')
    old_cost      = models.DecimalField(max_digits=12, decimal_places=4)
    new_cost      = models.DecimalField(max_digits=12, decimal_places=4)
    qty_restocked = models.DecimalField(max_digits=12, decimal_places=3)
    total_paid    = models.DecimalField(max_digits=12, decimal_places=2)
    recorded_by   = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    created_at    = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.ingredient.name} cost {self.old_cost}→{self.new_cost} on {self.created_at.date()}"

@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    """Automatically create a UserProfile when a new User is created."""
    if created:
        UserProfile.objects.get_or_create(user=instance)


# ── Activity Log ───────────────────────────────────────────────────────────────
class DamagedStock(models.Model):
    """Records ingredient waste/damage during cooking, linked to an order."""
    order        = models.ForeignKey('Order', null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name='damaged_items')
    ingredient   = models.ForeignKey(Ingredient, on_delete=models.CASCADE, related_name='damage_logs')
    qty_damaged  = models.DecimalField(max_digits=12, decimal_places=3)
    unit         = models.CharField(max_length=20)
    reason       = models.CharField(max_length=200, blank=True)
    recorded_by  = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    created_at   = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.ingredient.name} — {self.qty_damaged} {self.unit} damaged"


class ActivityLog(models.Model):
    ACTION_CHOICES = [
        # Auth
        ('login',              'Login'),
        ('logout',             'Logout'),
        # Reports
        ('view_dashboard',     'Viewed Dashboard'),
        ('view_sales_report',  'Viewed Sales Report'),
        ('view_shift_report',  'Viewed Shift Report'),
        ('view_stock_log',     'Viewed Stock Log'),
        ('view_cost_history',  'Viewed Cost History'),
        ('export_pdf',         'Exported PDF'),
        ('export_excel',       'Exported Excel'),
        # Menu
        ('add_menu_item',      'Added Menu Item'),
        ('edit_menu_item',     'Edited Menu Item'),
        ('delete_menu_item',   'Deleted Menu Item'),
        ('toggle_menu_item',   'Toggled Menu Item Status'),
        ('add_category',       'Added Category'),
        ('edit_category',      'Edited Category'),
        ('delete_category',    'Deleted Category'),
        # Ingredients
        ('add_ingredient',     'Added Ingredient'),
        ('edit_ingredient',    'Edited Ingredient'),
        ('delete_ingredient',  'Deleted Ingredient'),
        ('restock_ingredient', 'Restocked Ingredient'),
        # Orders
        ('place_order',        'Placed Order'),
        ('void_order',         'Voided Order'),
        ('delete_order',       'Deleted Order'),
        ('edit_order',         'Edited Order'),
        ('log_damage',         'Logged Damaged Stock'),
        # Employees
        ('add_employee',       'Added Employee'),
        ('edit_employee',      'Edited Employee'),
        ('delete_employee',    'Deleted Employee'),
        # Users
        ('add_user',           'Added User'),
        ('edit_user',          'Edited User'),
        ('delete_user',        'Deleted User'),
        # Promos
        ('add_promo',          'Added Promo Code'),
        ('edit_promo',         'Edited Promo Code'),
        ('delete_promo',       'Deleted Promo Code'),
        # Expenses
        ('add_expense',        'Added Expense'),
        ('edit_expense',       'Edited Expense'),
        ('delete_expense',     'Deleted Expense'),
    ]

    user       = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='activity_logs')
    action     = models.CharField(max_length=40, choices=ACTION_CHOICES)
    target     = models.CharField(max_length=200, blank=True)   # human-readable target, e.g. "Liver Sandwich"
    detail     = models.TextField(blank=True)                    # extra context
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user} — {self.get_action_display()} — {self.created_at:%Y-%m-%d %H:%M}"
