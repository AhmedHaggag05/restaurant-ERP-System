from django.db import migrations, models
import django.utils.timezone

class Migration(migrations.Migration):
    dependencies = [
        ('core', '0003_ingredient_cost_per_unit'),
    ]
    operations = [
        migrations.CreateModel(
            name='Employee',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('full_name', models.CharField(max_length=150)),
                ('phone', models.CharField(blank=True, max_length=30)),
                ('job_title', models.CharField(choices=[
                    ('manager','Manager'),('cashier','Cashier'),('chef','Chef'),
                    ('waiter','Waiter'),('cleaner','Cleaner'),('security','Security'),
                    ('delivery','Delivery'),('other','Other')],
                    default='cashier', max_length=20)),
                ('salary', models.DecimalField(decimal_places=2, default=0.0, max_digits=10)),
                ('hire_date', models.DateField(default=django.utils.timezone.now)),
                ('is_active', models.BooleanField(default=True)),
                ('notes', models.TextField(blank=True)),
            ],
        ),
    ]
