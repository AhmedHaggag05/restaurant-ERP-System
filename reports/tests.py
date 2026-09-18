import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from accounting.reporting import profit_loss_totals
from core.models import Ingredient, MenuItem, MenuItemIngredient, Order, PromoCode
from .views import _build_finance_custom, _build_finance_general


class FinancialReportTests(TestCase):
    def setUp(self):
        user = User.objects.create_user('report-manager', password='safe-test-password')
        user.profile.role = 'manager'
        user.profile.save()
        self.client.force_login(user)
        self.ingredient = Ingredient.objects.create(
            name='Flour', unit='kg', stock_quantity=Decimal('1.000'),
            cost_per_unit=Decimal('50.0000'))
        self.menu_item = MenuItem.objects.create(name='Sandwich', price=Decimal('20.00'))
        self.recipe = MenuItemIngredient.objects.create(
            menu_item=self.menu_item, ingredient=self.ingredient,
            quantity_required=Decimal('100.000'), recipe_unit='g')

    def test_discount_and_historical_cogs_match_posted_sales(self):
        PromoCode.objects.create(code='SAVE25', discount_type='percent',
                                 discount_value=Decimal('25.00'))
        response = self.client.post('/api/create-order/',
            data=json.dumps({'cart': [{'id': self.menu_item.pk, 'quantity': 2}],
                             'promo_code': 'SAVE25'}), content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        order = Order.objects.get()
        self.assertEqual(order.total_price, Decimal('30.00'))
        self.recipe.quantity_required = Decimal('500.000')
        self.recipe.save()
        self.ingredient.cost_per_unit = Decimal('200.0000')
        self.ingredient.save()

        pnl = profit_loss_totals()
        general = _build_finance_general('', '')
        self.assertEqual(pnl['sales'], Decimal('30.00'))
        self.assertEqual(pnl['cogs'], Decimal('10.00'))
        self.assertEqual(general['total_rev'], Decimal('30.00'))
        self.assertEqual(general['item_revenue'], Decimal('30.00'))
        self.assertEqual(general['item_cogs'], Decimal('10.0000'))
        self.assertEqual(general['net_profit'], Decimal('20.00'))
        custom = _build_finance_custom({'group_by': 'item'})
        self.assertEqual(custom['total_rev'], Decimal('30.00'))
        self.assertEqual(custom['total_cogs'], Decimal('10.0000'))
        for path in ('/sales-report/', '/sales-report/pdf/financial/',
                     '/reports/excel/financial/', '/reports/finance/general/',
                     '/reports/finance/general/pdf/', '/reports/finance/general/excel/',
                     '/reports/finance/custom/?group_by=item',
                     '/reports/finance/custom/pdf/?group_by=item',
                     '/reports/finance/custom/excel/?group_by=item'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
        self.menu_item.delete()
        general = _build_finance_general('', '')
        self.assertEqual(general['item_revenue'], Decimal('30.00'))
        self.assertEqual(general['item_cogs'], Decimal('10.0000'))
