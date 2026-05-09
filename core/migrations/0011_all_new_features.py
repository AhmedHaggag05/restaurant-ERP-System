from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone
from django.conf import settings

class Migration(migrations.Migration):
    dependencies = [
        ('core', '0010_ingredient_box_fields'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        # MenuCategory
        migrations.CreateModel(
            name='MenuCategory',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True)),
                ('name', models.CharField(max_length=60, unique=True)),
                ('sort_order', models.PositiveIntegerField(default=0)),
            ],
            options={'ordering': ['sort_order', 'name']},
        ),
        # MenuItem.category FK
        migrations.AddField(
            model_name='menuitem', name='category',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='items', to='core.menucategory'),
        ),
        # Order.status
        migrations.AddField(
            model_name='order', name='status',
            field=models.CharField(max_length=10, default='active',
                choices=[('active','Active'),('voided','Voided')]),
        ),
        migrations.AddField(
            model_name='order', name='voided_at',
            field=models.DateTimeField(null=True, blank=True),
        ),
        migrations.AddField(
            model_name='order', name='voided_by',
            field=models.ForeignKey(null=True, blank=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='voided_orders', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='order', name='void_reason',
            field=models.CharField(max_length=200, blank=True),
        ),
        # StockLog
        migrations.CreateModel(
            name='StockLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True)),
                ('reason', models.CharField(max_length=10,
                    choices=[('sale','Sale'),('restock','Restock'),('manual','Manual Adjustment'),('void','Order Void')])),
                ('qty_before', models.DecimalField(max_digits=12, decimal_places=3)),
                ('qty_change', models.DecimalField(max_digits=12, decimal_places=3)),
                ('qty_after',  models.DecimalField(max_digits=12, decimal_places=3)),
                ('note', models.CharField(max_length=200, blank=True)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('changed_by', models.ForeignKey(null=True, blank=True,
                    on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
                ('ingredient', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                    related_name='stock_logs', to='core.ingredient')),
            ],
            options={'ordering': ['-created_at']},
        ),
        # CostHistory
        migrations.CreateModel(
            name='CostHistory',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True)),
                ('old_cost', models.DecimalField(max_digits=12, decimal_places=4)),
                ('new_cost', models.DecimalField(max_digits=12, decimal_places=4)),
                ('qty_restocked', models.DecimalField(max_digits=12, decimal_places=3)),
                ('total_paid', models.DecimalField(max_digits=12, decimal_places=2)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('ingredient', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                    related_name='cost_history', to='core.ingredient')),
                ('recorded_by', models.ForeignKey(null=True, blank=True,
                    on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
            options={'ordering': ['-created_at']},
        ),
    ]
