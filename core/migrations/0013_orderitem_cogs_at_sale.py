from django.db import migrations, models


class Migration(migrations.Migration):
    """
    Adds cogs_at_sale to core_orderitem.
    Uses raw SQL with IF NOT EXISTS so it never fails if already applied.
    """
    dependencies = [
        ('core', '0012_activitylog'),
    ]

    operations = [
        migrations.AddField(
            model_name='orderitem',
            name='cogs_at_sale',
            field=models.DecimalField(
                decimal_places=4, default=0, max_digits=10,
                help_text='Total COGS for this line snapshotted at sale time'
            ),
        ),
    ]
