from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase

from accounting.models import JournalEntry, JournalLine
from core.models import Employee
from .models import Advance, SalaryLine, SalaryRun
from .payroll import mark_salary_run_paid, pay_advance


class PayrollIntegrityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('manager', password='safe-test-password')
        self.user.profile.role = 'manager'
        self.user.profile.save()
        self.client.force_login(self.user)
        self.employee = Employee.objects.create(full_name='Alex', salary=Decimal('100.00'))
        self.run = SalaryRun.objects.create(
            period_start=date(2026, 1, 1), period_end=date(2026, 1, 31),
            total_gross=Decimal('100.00'), total_deductions=Decimal('10.00'),
            total_net=Decimal('90.00'))
        self.line = SalaryLine.objects.create(
            salary_run=self.run, employee=self.employee,
            base_salary=Decimal('100.00'),
            gross=Decimal('100.00'), deductions=Decimal('10.00'),
            advances=Decimal('0'), net=Decimal('90.00'))

    def test_salary_reduction_posts_balanced_journal_and_paid_line_is_immutable(self):
        mark_salary_run_paid(self.run.pk, self.user)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, 'paid')
        entry = JournalEntry.objects.get(source='payroll', source_id=self.run.pk)
        self.assertTrue(entry.is_balanced())
        self.assertEqual(JournalLine.objects.get(journal_entry=entry, side='debit').amount,
                         Decimal('90.00'))
        with self.assertRaisesMessage(ValueError, 'already paid'):
            mark_salary_run_paid(self.run.pk, self.user)
        self.client.post(f'/hr/payroll/{self.run.pk}/', {
            'action': 'edit_line', 'line_id': self.line.pk,
            'gross': '200', 'deductions': '0',
        })
        self.line.refresh_from_db()
        self.assertEqual(self.line.gross, Decimal('100.00'))

    def test_failed_journal_rolls_back_paid_status(self):
        with patch('hr.payroll.post_payroll_journal', side_effect=RuntimeError('test failure')):
            with self.assertRaises(RuntimeError):
                mark_salary_run_paid(self.run.pk, self.user)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, 'draft')

    def test_advance_larger_than_pay_is_not_silently_written_off(self):
        Advance.objects.create(employee=self.employee, amount=Decimal('150.00'), status='paid')
        self.client.post('/hr/payroll/create/', {
            'period_start': '2026-02-01', 'period_end': '2026-02-28',
        })
        self.assertEqual(SalaryRun.objects.count(), 1)
        self.assertFalse(Advance.objects.filter(deducted_in__isnull=False).exists())

    def test_approval_does_not_deduct_until_separate_payment(self):
        advance = Advance.objects.create(employee=self.employee, amount=Decimal('30.00'), status='approved')
        self.client.post('/hr/payroll/create/', {
            'period_start': '2026-02-01', 'period_end': '2026-02-28',
        })
        advance.refresh_from_db()
        self.assertIsNone(advance.deducted_in)
        self.assertEqual(SalaryRun.objects.count(), 2)
        paid = pay_advance(advance.pk, self.user)
        self.assertEqual(paid.status, 'paid')
        self.assertIsNotNone(paid.paid_at)
        entry = JournalEntry.objects.get(source='advance', source_id=advance.pk)
        self.assertTrue(entry.is_balanced())
        self.assertEqual(JournalLine.objects.get(journal_entry=entry, side='debit').account.code, '5300')
        self.client.post('/hr/payroll/create/', {
            'period_start': '2026-03-01', 'period_end': '2026-03-31',
        })
        advance.refresh_from_db()
        self.assertIsNotNone(advance.deducted_in)
        self.assertEqual(advance.deducted_in.lines.get().advances, Decimal('30.00'))
        with self.assertRaisesMessage(ValueError, 'approved, unpaid'):
            pay_advance(advance.pk, self.user)

    def test_failed_advance_journal_rolls_back_payment(self):
        advance = Advance.objects.create(employee=self.employee, amount=Decimal('30.00'), status='approved')
        with patch('hr.payroll.post_advance_payment_journal', side_effect=RuntimeError('test failure')):
            with self.assertRaises(RuntimeError):
                pay_advance(advance.pk, self.user)
        advance.refresh_from_db()
        self.assertEqual(advance.status, 'approved')
        self.assertIsNone(advance.paid_at)

    def test_old_draft_reservation_can_be_released_without_payment(self):
        advance = Advance.objects.create(
            employee=self.employee, amount=Decimal('30.00'),
            status='approved', deducted_in=self.run)
        self.line.advances = Decimal('30.00')
        self.line.net = Decimal('60.00')
        self.line.save()
        self.run.total_net = Decimal('60.00')
        self.run.save()
        self.client.post(f'/hr/payroll/{self.run.pk}/', {
            'action': 'edit_line', 'line_id': self.line.pk,
            'gross': '-1.00', 'deductions': '10.00',
        })
        advance.refresh_from_db()
        self.assertEqual(advance.deducted_in_id, self.run.pk)
        self.client.post(f'/hr/payroll/{self.run.pk}/', {
            'action': 'edit_line', 'line_id': self.line.pk,
            'gross': '100.00', 'deductions': '10.00',
        })
        advance.refresh_from_db()
        self.line.refresh_from_db()
        self.assertIsNone(advance.deducted_in)
        self.assertEqual(self.line.advances, Decimal('0.00'))
        self.assertEqual(self.line.net, Decimal('90.00'))
