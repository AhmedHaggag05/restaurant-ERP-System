from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Account',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True)),
                ('code', models.CharField(max_length=10, unique=True)),
                ('name', models.CharField(max_length=150)),
                ('account_type', models.CharField(choices=[('asset','Asset'),('liability','Liability'),('equity','Equity'),('revenue','Revenue'),('expense','Expense')], max_length=12)),
                ('description', models.TextField(blank=True)),
                ('is_active', models.BooleanField(default=True)),
                ('is_system', models.BooleanField(default=False)),
                ('parent', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='children', to='accounting.account')),
            ],
            options={'ordering': ['code']},
        ),
        migrations.CreateModel(
            name='JournalEntry',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True)),
                ('entry_number', models.CharField(editable=False, max_length=20, unique=True)),
                ('date', models.DateField(default=django.utils.timezone.now)),
                ('description', models.CharField(max_length=255)),
                ('source', models.CharField(choices=[('sale','Sale'),('purchase','Purchase / Restock'),('payroll','Payroll'),('expense','Expense'),('manual','Manual'),('void','Order Void'),('advance','Employee Advance')], default='manual', max_length=12)),
                ('source_id', models.PositiveIntegerField(blank=True, null=True)),
                ('is_posted', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
            options={'ordering': ['-date', '-created_at']},
        ),
        migrations.CreateModel(
            name='JournalLine',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True)),
                ('side', models.CharField(choices=[('debit','Debit'),('credit','Credit')], max_length=6)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=14)),
                ('description', models.CharField(blank=True, max_length=200)),
                ('account', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='journal_lines', to='accounting.account')),
                ('journal_entry', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='lines', to='accounting.journalentry')),
            ],
        ),
    ]
