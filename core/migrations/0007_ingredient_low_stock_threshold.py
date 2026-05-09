from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [('core', '0006_promocode_expense_order_extras')]
    operations = [
        migrations.AddField(
            model_name='ingredient',
            name='low_stock_threshold',
            field=models.DecimalField(decimal_places=2, default=10.0, max_digits=10),
        ),
    ]
