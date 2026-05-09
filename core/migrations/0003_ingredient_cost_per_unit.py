from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0002_userprofile'),
    ]

    operations = [
        migrations.AddField(
            model_name='ingredient',
            name='cost_per_unit',
            field=models.DecimalField(decimal_places=2, default=0.0, max_digits=10),
        ),
    ]
