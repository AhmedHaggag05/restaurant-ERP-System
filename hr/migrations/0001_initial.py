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
            name='AttendanceRecord',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True)),
                ('date', models.DateField(default=django.utils.timezone.now)),
                ('status', models.CharField(choices=[('present','Present'),('absent','Absent'),('late','Late'),('half_day','Half Day'),('leave','On Leave')], default='present', max_length=10)),
                ('check_in', models.TimeField(blank=True, null=True)),
                ('check_out', models.TimeField(blank=True, null=True)),
                ('notes', models.CharField(blank=True, max_length=200)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('employee', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='attendance', to='core.employee')),
                ('recorded_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
            options={'ordering': ['-date'], 'unique_together': {('employee', 'date')}},
        ),
        migrations.CreateModel(
            name='SalaryRun',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True)),
                ('period_start', models.DateField()),
                ('period_end', models.DateField()),
                ('run_date', models.DateField(default=django.utils.timezone.now)),
                ('status', models.CharField(choices=[('draft','Draft'),('paid','Paid')], default='draft', max_length=10)),
                ('total_gross', models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ('total_deductions', models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ('total_net', models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ('notes', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
            options={'ordering': ['-period_end']},
        ),
        migrations.CreateModel(
            name='SalaryLine',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True)),
                ('base_salary', models.DecimalField(decimal_places=2, max_digits=10)),
                ('days_worked', models.DecimalField(decimal_places=1, default=0, max_digits=5)),
                ('days_total', models.DecimalField(decimal_places=1, default=30, max_digits=5)),
                ('gross', models.DecimalField(decimal_places=2, default=0, max_digits=10)),
                ('deductions', models.DecimalField(decimal_places=2, default=0, max_digits=10)),
                ('advances', models.DecimalField(decimal_places=2, default=0, max_digits=10)),
                ('net', models.DecimalField(decimal_places=2, default=0, max_digits=10)),
                ('notes', models.CharField(blank=True, max_length=200)),
                ('employee', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='core.employee')),
                ('salary_run', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='lines', to='hr.salaryrun')),
            ],
        ),
        migrations.CreateModel(
            name='Advance',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=10)),
                ('reason', models.CharField(blank=True, max_length=200)),
                ('date', models.DateField(default=django.utils.timezone.now)),
                ('status', models.CharField(choices=[('pending','Pending'),('approved','Approved'),('rejected','Rejected'),('deducted','Deducted')], default='pending', max_length=10)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('employee', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='advances', to='core.employee')),
                ('approved_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='approved_advances', to=settings.AUTH_USER_MODEL)),
                ('deducted_in', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='deducted_advances', to='hr.salaryrun')),
            ],
            options={'ordering': ['-date']},
        ),
    ]
