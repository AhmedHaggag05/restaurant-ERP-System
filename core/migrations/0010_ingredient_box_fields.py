from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [('core', '0009_menuitemingredient_recipe_unit')]
    operations = [
        migrations.AddField(
            model_name='ingredient', name='units_per_box',
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddField(
            model_name='ingredient', name='sub_unit',
            field=models.CharField(max_length=10, default='packets',
                choices=[('cans','Cans'),('packets','Packets')]),
        ),
        migrations.AlterField(
            model_name='ingredient', name='stock_quantity',
            field=models.DecimalField(max_digits=12, decimal_places=3, default=0),
        ),
        migrations.AlterField(
            model_name='ingredient', name='cost_per_unit',
            field=models.DecimalField(max_digits=12, decimal_places=4, default=0),
        ),
        migrations.AlterField(
            model_name='ingredient', name='low_stock_threshold',
            field=models.DecimalField(max_digits=10, decimal_places=3, default=10),
        ),
        migrations.AlterField(
            model_name='menuitemingredient',
            name='recipe_unit',
            field=models.CharField(max_length=10, default='g',
                choices=[('g','grams'),('mL','mL'),('packets','packets'),
                         ('cans','cans'),('pieces','pieces')]),
        ),
    ]
