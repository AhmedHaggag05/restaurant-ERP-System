from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('core', '0018_remove_orderitem_safe_manager')]

    operations = [
        migrations.AlterModelOptions(
            name='orderitem', options={'base_manager_name': 'objects'},
        ),
        migrations.AddField(
            model_name='orderitem',
            name='stock_snapshot_ready',
            field=models.BooleanField(default=False),
        ),
        migrations.CreateModel(
            name='OrderItemConsumption',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('quantity_consumed', models.DecimalField(decimal_places=3, max_digits=12)),
                ('ingredient', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='core.ingredient')),
                ('order_item', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='consumptions', to='core.orderitem')),
            ],
        ),
        migrations.AddConstraint(
            model_name='orderitemconsumption',
            constraint=models.UniqueConstraint(fields=('order_item', 'ingredient'), name='unique_order_item_ingredient_consumption'),
        ),
    ]
