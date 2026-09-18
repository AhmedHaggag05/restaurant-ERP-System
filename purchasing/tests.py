from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db.models import Sum
from django.test import TestCase

from accounting.models import JournalLine
from core.models import Ingredient
from .models import PurchaseOrder, PurchaseOrderLine, Supplier, SupplierInvoice
from .receiving import receive_goods


class GoodsReceivingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('manager', password='safe-test-password')
        self.user.profile.role = 'manager'
        self.user.profile.save()
        self.ingredient = Ingredient.objects.create(
            name='Tomatoes', unit='kg', stock_quantity=Decimal('0'),
            cost_per_unit=Decimal('0'))
        self.supplier = Supplier.objects.create(name='Farm')
        self.po = PurchaseOrder.objects.create(supplier=self.supplier, total_amount=Decimal('30.00'))
        self.line = PurchaseOrderLine.objects.create(
            purchase_order=self.po, ingredient=self.ingredient,
            quantity_ordered=Decimal('10.000'), unit_cost=Decimal('3.0000'))

    def test_partial_receipts_post_only_received_value(self):
        receive_goods(self.po.pk, {f'received_{self.line.pk}': '4'}, self.user)
        self.po.refresh_from_db()
        self.ingredient.refresh_from_db()
        self.assertEqual(self.po.status, 'partial')
        self.assertEqual(self.ingredient.stock_quantity, Decimal('4.000'))
        self.assertEqual(list(SupplierInvoice.objects.values_list('amount', flat=True)),
                         [Decimal('12.00')])

        receive_goods(self.po.pk, {f'received_{self.line.pk}': '6'}, self.user)
        self.po.refresh_from_db()
        self.ingredient.refresh_from_db()
        self.assertEqual(self.po.status, 'received')
        self.assertEqual(self.ingredient.stock_quantity, Decimal('10.000'))
        self.assertEqual(SupplierInvoice.objects.count(), 2)
        self.assertEqual(SupplierInvoice.objects.aggregate(total=Sum('amount'))['total'],
                         Decimal('30.00'))
        self.assertEqual(JournalLine.objects.filter(account__code='1200', side='debit')
                         .aggregate(total=Sum('amount'))['total'],
                         Decimal('30.00'))
        with self.assertRaisesMessage(ValueError, 'cannot receive'):
            receive_goods(self.po.pk, {f'received_{self.line.pk}': '1'}, self.user)

    def test_journal_failure_rolls_back_receipt(self):
        with patch('purchasing.receiving.post_purchase_journal', side_effect=RuntimeError('test failure')):
            with self.assertRaises(RuntimeError):
                receive_goods(self.po.pk, {f'received_{self.line.pk}': '4'}, self.user)
        self.line.refresh_from_db()
        self.ingredient.refresh_from_db()
        self.assertEqual(self.line.quantity_received, Decimal('0'))
        self.assertEqual(self.ingredient.stock_quantity, Decimal('0'))
        self.assertFalse(SupplierInvoice.objects.exists())
