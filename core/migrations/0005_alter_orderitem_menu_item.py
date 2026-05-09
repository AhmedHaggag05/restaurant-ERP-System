from django.db import migrations, models
import django.db.models.deletion

class Migration(migrations.Migration):
    dependencies = [
        ('core', '0004_employee'),
    ]
    operations = [
        migrations.AlterField(
            model_name='orderitem',
            name='menu_item',
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to='core.menuitem',
            ),
        ),
    ]
