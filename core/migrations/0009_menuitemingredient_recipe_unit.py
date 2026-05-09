from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [('core', '0008_userprofile_superadmin')]
    operations = [
        migrations.AddField(
            model_name='menuitemingredient',
            name='recipe_unit',
            field=models.CharField(
                choices=[('g','grams (g)'),('mL','milliliters (mL)'),('packets','packets'),('pieces','pieces')],
                default='g', max_length=10,
            ),
        ),
        migrations.AlterField(
            model_name='menuitemingredient',
            name='quantity_required',
            field=models.DecimalField(decimal_places=3, max_digits=10),
        ),
    ]
