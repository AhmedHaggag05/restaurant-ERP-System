import json
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db.models import Sum
from django.test import TestCase

from accounting.models import JournalEntry, JournalLine
from .models import Expense, Ingredient, MenuItem, MenuItemIngredient, Order, OrderItem, StockLog
from .order_integrity import edit_existing_order, void_existing_order


class OrderIntegrityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('manager', password='safe-test-password')
        self.user.profile.role = 'manager'
        self.user.profile.save()
        self.client.force_login(self.user)
        self.ingredient = Ingredient.objects.create(
            name='Flour', unit='kg', stock_quantity=Decimal('1.000'),
            cost_per_unit=Decimal('50.0000'))
        self.menu_item = MenuItem.objects.create(name='Sandwich', price=Decimal('20.00'))
        self.recipe = MenuItemIngredient.objects.create(
            menu_item=self.menu_item, ingredient=self.ingredient,
            quantity_required=Decimal('100.000'), recipe_unit='g')

    def place_order(self, quantity=2):
        response = self.client.post('/api/create-order/',
            data=json.dumps({'cart': [{'id': self.menu_item.pk, 'quantity': quantity}]}),
            content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        return Order.objects.get()

    def account_balance(self, code):
        lines = JournalLine.objects.filter(account__code=code)
        debit = lines.filter(side='debit').aggregate(total=Sum('amount'))['total'] or Decimal('0')
        credit = lines.filter(side='credit').aggregate(total=Sum('amount'))['total'] or Decimal('0')
        return debit - credit

    def test_order_edit_and_void_use_consumption_snapshot_and_reverse_journals(self):
        order = self.place_order()
        item = order.items.get()
        self.ingredient.refresh_from_db()
        self.assertEqual(self.ingredient.stock_quantity, Decimal('0.800'))
        self.assertEqual(item.consumptions.get().quantity_consumed, Decimal('0.200'))
        self.assertEqual(self.account_balance('4000'), Decimal('-40.00'))

        # A later recipe edit must not alter stock restoration for the original sale.
        self.recipe.quantity_required = Decimal('200.000')
        self.recipe.save()
        edit_existing_order(order.pk, {f'qty_{item.pk}': '3'}, self.user)
        self.ingredient.refresh_from_db()
        self.assertEqual(self.ingredient.stock_quantity, Decimal('0.600'))
        edit_existing_order(order.pk, {f'qty_{item.pk}': '1'}, self.user)
        self.ingredient.refresh_from_db()
        self.assertEqual(self.ingredient.stock_quantity, Decimal('0.867'))

        void_existing_order(order.pk, 'Customer cancelled', self.user)
        self.ingredient.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(self.ingredient.stock_quantity, Decimal('1.000'))
        self.assertEqual(order.status, 'voided')
        self.assertEqual(self.account_balance('4000'), Decimal('0.00'))
        self.assertEqual(self.account_balance('5000'), Decimal('0.00'))
        self.assertEqual(StockLog.objects.filter(reason='void').count(), 1)

    def test_insufficient_stock_on_edit_rolls_back_everything(self):
        order = self.place_order()
        item = order.items.get()
        with self.assertRaisesMessage(ValueError, 'Insufficient stock'):
            edit_existing_order(order.pk, {f'qty_{item.pk}': '99'}, self.user)
        order.refresh_from_db()
        item.refresh_from_db()
        self.ingredient.refresh_from_db()
        self.assertEqual((order.total_price, item.quantity, self.ingredient.stock_quantity),
                         (Decimal('40.00'), 2, Decimal('0.800')))
        self.assertEqual(JournalEntry.objects.count(), 2)

    def test_sale_journal_failure_rolls_back_order_and_stock(self):
        with patch('accounting.utils.post_sale_journal', side_effect=RuntimeError('test failure')):
            response = self.client.post('/api/create-order/',
                data=json.dumps({'cart': [{'id': self.menu_item.pk, 'quantity': 1}]}),
                content_type='application/json')
        self.assertEqual(response.status_code, 500)
        self.assertFalse(Order.objects.exists())
        self.ingredient.refresh_from_db()
        self.assertEqual(self.ingredient.stock_quantity, Decimal('1.000'))
        self.assertFalse(StockLog.objects.exists())

    def test_legacy_order_quantity_edit_is_rejected(self):
        order = Order.objects.create(total_price=Decimal('20.00'))
        item = OrderItem.objects.create(order=order, menu_item=self.menu_item,
                                        quantity=1, price_at_order=Decimal('20.00'))
        with self.assertRaisesMessage(ValueError, 'no per-item stock snapshot'):
            edit_existing_order(order.pk, {f'qty_{item.pk}': '2'}, self.user)
        item.refresh_from_db()
        self.assertEqual(item.quantity, 1)


class ExpenseIntegrityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('expense-manager', password='safe-test-password')
        self.user.profile.role = 'manager'
        self.user.profile.save()
        self.client.force_login(self.user)

    def test_expense_void_keeps_record_and_reverses_journal(self):
        response = self.client.post('/expenses/add/', {
            'title': 'Rent', 'category': 'rent', 'amount': '125.00',
            'date': '2026-09-18',
        })
        self.assertEqual(response.status_code, 302)
        expense = Expense.objects.get()
        self.assertEqual(JournalEntry.objects.filter(source='expense').count(), 1)

        response = self.client.post(f'/expenses/edit/{expense.pk}/', {
            'title': 'Rent', 'amount': '200.00', 'date': '2026-09-18',
        })
        self.assertEqual(response.status_code, 302)
        expense.refresh_from_db()
        self.assertEqual(expense.amount, Decimal('125.00'))

        self.client.get(f'/expenses/delete/{expense.pk}/')
        expense.refresh_from_db()
        self.assertFalse(expense.is_voided)
        self.client.post(f'/expenses/delete/{expense.pk}/')
        expense.refresh_from_db()
        self.assertTrue(expense.is_voided)
        self.assertEqual(JournalEntry.objects.filter(source='expense').count(), 2)
        debits = JournalLine.objects.filter(account__code='5200', side='debit').aggregate(t=Sum('amount'))['t']
        credits = JournalLine.objects.filter(account__code='5200', side='credit').aggregate(t=Sum('amount'))['t']
        self.assertEqual(debits, credits)

    def test_expense_journal_failure_rolls_back_record(self):
        with patch('accounting.utils.post_expense_journal', side_effect=RuntimeError('test failure')):
            self.client.post('/expenses/add/', {'title': 'Rent', 'amount': '125.00'})
        self.assertFalse(Expense.objects.exists())
