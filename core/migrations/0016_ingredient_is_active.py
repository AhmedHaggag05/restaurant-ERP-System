from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0015_promocode_expiry'),
    ]

    operations = [
        migrations.AddField(
            model_name='ingredient',
            name='is_active',
            field=models.BooleanField(
                default=True,
                help_text='Inactive ingredients are hidden from POS and new recipes but history is preserved'
            ),
        ),
    ]
