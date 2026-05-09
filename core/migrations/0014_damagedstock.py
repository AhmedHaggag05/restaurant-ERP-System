from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0013_orderitem_cogs_at_sale'),
    ]

    operations = [
        # Extend reason field to accommodate new choices
        migrations.AlterField(
            model_name='stocklog',
            name='reason',
            field=models.CharField(
                max_length=12,
                choices=[
                    ('sale',    'Sale'),
                    ('restock', 'Restock'),
                    ('manual',  'Manual Adjustment'),
                    ('void',    'Order Void'),
                    ('damaged', 'Damaged / Wastage'),
                    ('edit',    'Order Edit'),
                ],
            ),
        ),
        # DamagedStock table
        migrations.CreateModel(
            name='DamagedStock',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('qty_damaged', models.DecimalField(decimal_places=3, max_digits=12)),
                ('unit', models.CharField(max_length=20)),
                ('reason', models.CharField(blank=True, max_length=200)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('ingredient', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='damage_logs',
                    to='core.ingredient',
                )),
                ('order', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='damaged_items',
                    to='core.order',
                )),
                ('recorded_by', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    to='auth.user',
                )),
            ],
            options={'ordering': ['-created_at']},
        ),
    ]
