from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0017_alter_orderitem_options_alter_activitylog_action_and_more'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='orderitem',
            options={},
        ),
    ]