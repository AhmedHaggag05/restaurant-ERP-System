from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('core', '0012_activitylog'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Supplier',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True)),
                ('name', models.CharField(max_length=150, unique=True)),
                ('contact', models.CharField(blank=True, max_length=100)),
                ('phone', models.CharField(blank=True, max_length=30)),
                ('email', models.EmailField(blank=True)),
                ('address', models.TextField(blank=True)),
                ('notes', models.TextField(blank=True)),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.CreateModel(
            name='PurchaseOrder',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True)),
                ('po_number', models.CharField(editable=False, max_length=20, unique=True)),
                ('status', models.CharField(choices=[('draft','Draft'),('sent','Sent to Supplier'),('received','Received'),('partial','Partially Received'),('cancelled','Cancelled')], default='draft', max_length=12)),
                ('order_date', models.DateField(default=django.utils.timezone.now)),
                ('expected_date', models.DateField(blank=True, null=True)),
                ('received_date', models.DateField(blank=True, null=True)),
                ('notes', models.TextField(blank=True)),
                ('total_amount', models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('supplier', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='purchase_orders', to='purchasing.supplier')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='purchase_orders', to=settings.AUTH_USER_MODEL)),
            ],
            options={'ordering': ['-created_at']},
        ),
        migrations.CreateModel(
            name='PurchaseOrderLine',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True)),
                ('quantity_ordered', models.DecimalField(decimal_places=3, max_digits=12)),
                ('quantity_received', models.DecimalField(decimal_places=3, default=0, max_digits=12)),
                ('unit_cost', models.DecimalField(decimal_places=4, max_digits=12)),
                ('total_cost', models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ('notes', models.CharField(blank=True, max_length=200)),
                ('purchase_order', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='lines', to='purchasing.purchaseorder')),
                ('ingredient', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='core.ingredient')),
            ],
        ),
        migrations.CreateModel(
            name='SupplierInvoice',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True)),
                ('invoice_number', models.CharField(blank=True, max_length=60)),
                ('invoice_date', models.DateField(default=django.utils.timezone.now)),
                ('due_date', models.DateField(blank=True, null=True)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=12)),
                ('amount_paid', models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ('status', models.CharField(choices=[('unpaid','Unpaid'),('paid','Paid'),('partial','Partially Paid')], default='unpaid', max_length=10)),
                ('notes', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('supplier', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='purchasing.supplier')),
                ('purchase_order', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='invoices', to='purchasing.purchaseorder')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
            options={'ordering': ['-invoice_date']},
        ),
    ]
