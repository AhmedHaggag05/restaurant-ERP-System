from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone

class Migration(migrations.Migration):
    dependencies = [('core', '0005_alter_orderitem_menu_item')]
    operations = [
        migrations.CreateModel('PromoCode', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
            ('code', models.CharField(max_length=30, unique=True)),
            ('description', models.CharField(blank=True, max_length=100)),
            ('discount_type', models.CharField(choices=[('percent','Percent %'),('fixed','Fixed EGP')], default='percent', max_length=10)),
            ('discount_value', models.DecimalField(decimal_places=2, max_digits=8)),
            ('is_active', models.BooleanField(default=True)),
            ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
        ]),
        migrations.CreateModel('Expense', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
            ('title', models.CharField(max_length=150)),
            ('category', models.CharField(choices=[('rent','Rent'),('utilities','Utilities'),('packaging','Packaging'),('maintenance','Maintenance'),('marketing','Marketing'),('other','Other')], default='other', max_length=20)),
            ('amount', models.DecimalField(decimal_places=2, max_digits=10)),
            ('date', models.DateField(default=django.utils.timezone.now)),
            ('notes', models.TextField(blank=True)),
        ]),
        migrations.AddField(model_name='order', name='table_number',
            field=models.CharField(blank=True, max_length=10)),
        migrations.AddField(model_name='order', name='discount_amount',
            field=models.DecimalField(decimal_places=2, default=0.0, max_digits=8)),
        migrations.AddField(model_name='order', name='promo_code',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='core.promocode')),
    ]
