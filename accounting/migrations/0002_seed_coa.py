from django.db import migrations


def seed_coa(apps, schema_editor):
    Account = apps.get_model('accounting', 'Account')
    defaults = [
        ('1000', 'Cash / Bank',           'asset',    True),
        ('1100', 'Accounts Receivable',   'asset',    True),
        ('1200', 'Inventory / Stock',     'asset',    True),
        ('2000', 'Accounts Payable',      'liability', True),
        ('3000', 'Retained Earnings',     'equity',   True),
        ('4000', 'Sales Revenue',         'revenue',  True),
        ('5000', 'Cost of Goods Sold',    'expense',  True),
        ('5100', 'Payroll Expense',       'expense',  True),
        ('5200', 'Operating Expenses',    'expense',  True),
        ('5300', 'Employee Advances',     'asset',    True),
    ]
    for code, name, atype, is_system in defaults:
        Account.objects.get_or_create(
            code=code,
            defaults={'name': name, 'account_type': atype, 'is_system': is_system, 'is_active': True}
        )


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(seed_coa, migrations.RunPython.noop),
    ]
