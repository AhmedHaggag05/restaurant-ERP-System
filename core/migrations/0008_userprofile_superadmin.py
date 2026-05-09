from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [('core', '0007_ingredient_low_stock_threshold')]
    operations = [
        migrations.AlterField(
            model_name='userprofile',
            name='role',
            field=models.CharField(
                choices=[('superadmin','Super Admin'),('manager','Manager'),('cashier','Cashier')],
                default='cashier', max_length=12,
            ),
        ),
    ]
