from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0011_all_new_features'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ActivityLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('action', models.CharField(choices=[
                    ('login','Login'),('logout','Logout'),
                    ('view_dashboard','Viewed Dashboard'),('view_sales_report','Viewed Sales Report'),
                    ('view_shift_report','Viewed Shift Report'),('view_stock_log','Viewed Stock Log'),
                    ('view_cost_history','Viewed Cost History'),
                    ('export_pdf','Exported PDF'),('export_excel','Exported Excel'),
                    ('add_menu_item','Added Menu Item'),('edit_menu_item','Edited Menu Item'),
                    ('delete_menu_item','Deleted Menu Item'),('toggle_menu_item','Toggled Menu Item Status'),
                    ('add_category','Added Category'),('edit_category','Edited Category'),('delete_category','Deleted Category'),
                    ('add_ingredient','Added Ingredient'),('edit_ingredient','Edited Ingredient'),
                    ('delete_ingredient','Deleted Ingredient'),('restock_ingredient','Restocked Ingredient'),
                    ('void_order','Voided Order'),('delete_order','Deleted Order'),
                    ('add_employee','Added Employee'),('edit_employee','Edited Employee'),('delete_employee','Deleted Employee'),
                    ('add_user','Added User'),('edit_user','Edited User'),('delete_user','Deleted User'),
                    ('add_promo','Added Promo Code'),('edit_promo','Edited Promo Code'),('delete_promo','Deleted Promo Code'),
                    ('add_expense','Added Expense'),('edit_expense','Edited Expense'),('delete_expense','Deleted Expense'),
                ], max_length=40)),
                ('target', models.CharField(blank=True, max_length=200)),
                ('detail', models.TextField(blank=True)),
                ('ip_address', models.GenericIPAddressField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('user', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='activity_logs', to=settings.AUTH_USER_MODEL)),
            ],
            options={'ordering': ['-created_at']},
        ),
    ]
