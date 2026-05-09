from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0014_damagedstock'),
    ]

    operations = [
        migrations.AddField(
            model_name='promocode',
            name='valid_from',
            field=models.DateField(blank=True, null=True,
                help_text='Start date (leave blank = always valid from start)'),
        ),
        migrations.AddField(
            model_name='promocode',
            name='valid_until',
            field=models.DateField(blank=True, null=True,
                help_text='Expiry date (leave blank = never expires)'),
        ),
        migrations.AddField(
            model_name='promocode',
            name='max_uses',
            field=models.PositiveIntegerField(blank=True, null=True,
                help_text='Max total uses (blank = unlimited)'),
        ),
        migrations.AddField(
            model_name='promocode',
            name='times_used',
            field=models.PositiveIntegerField(default=0),
        ),
    ]
