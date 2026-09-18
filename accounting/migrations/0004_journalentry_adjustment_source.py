from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('accounting', '0003_alter_account_id_alter_journalentry_id_and_more')]

    operations = [
        migrations.AlterField(
            model_name='journalentry',
            name='source',
            field=models.CharField(choices=[
                ('sale', 'Sale'), ('purchase', 'Purchase / Restock'),
                ('payroll', 'Payroll'), ('expense', 'Expense'),
                ('manual', 'Manual'), ('void', 'Order Void'),
                ('adjustment', 'Order / Expense Adjustment'),
                ('advance', 'Employee Advance'),
                ('damage', 'Damaged Goods / Wastage'),
            ], default='manual', max_length=12),
        ),
    ]
