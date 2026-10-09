# -*- coding: utf-8 -*-
"""Invoice/wizard flow tests with the SOAP layer mocked.

NOTE ON COMMITS: the payment wizard deliberately calls self.env.cr.commit()
and opens fresh cursors (see wizard/afs_payment_wizard.py, §5 of the design
notes) so an orphaned 'pending' record survives a worker crash instead of
rolling back invisibly. Odoo's TransactionCase monkeypatches cr.commit() to
raise, specifically to stop tests from doing this — for real usage this is
correct, so _confirm() below neutralises that guard for the one call that
needs it, rather than changing the production code to suit the test harness.
The fresh cursors the wizard opens for its actual writes are real, separate
connections and commit for real regardless — run only against a disposable
test database (e.g. via `--test-enable`), never against production data.
"""
import unittest
from unittest.mock import patch

import requests

from odoo.exceptions import UserError
from odoo.tests import Form, TransactionCase, tagged

from ..services import afs_client

APPROVED_XML = """<?xml version="1.0"?>
<SaleResponse xmlns:a="http://schemas.datacontract.org/2004/07/">
  <SaleResult>
    <a:PosAmount>{amount}</a:PosAmount>
    <a:PosAuthCode>974086</a:PosAuthCode>
    <a:PosBatchNumber>001</a:PosBatchNumber>
    <a:PosCurrencyCode>784</a:PosCurrencyCode>
    <a:PosDate>20260916</a:PosDate>
    <a:PosEmvAppName>Visa DEBIT</a:PosEmvAppName>
    <a:PosInvoiceNumber>000006</a:PosInvoiceNumber>
    <a:PosIssuerName>VISA</a:PosIssuerName>
    <a:PosPan>408578******5322</a:PosPan>
    <a:PosRRN>625969470006</a:PosRRN>
    <a:PosRespCode>00</a:PosRespCode>
    <a:PosRespStatus>1</a:PosRespStatus>
    <a:PosRespText>Approved</a:PosRespText>
    <a:PosStan>000006</a:PosStan>
    <a:PosTime>110027</a:PosTime>
    <a:WebResponseStatus>Success</a:WebResponseStatus>
  </SaleResult>
</SaleResponse>"""

DECLINED_XML = """<?xml version="1.0"?>
<SaleResponse xmlns:a="http://schemas.datacontract.org/2004/07/">
  <SaleResult>
    <a:PosRespCode>05</a:PosRespCode>
    <a:PosRespText>Declined</a:PosRespText>
    <a:PosRespStatus>0</a:PosRespStatus>
    <a:WebResponseStatus>Success</a:WebResponseStatus>
  </SaleResult>
</SaleResponse>"""

VOID_APPROVED_XML = """<?xml version="1.0"?>
<VoidResponse xmlns:a="http://schemas.datacontract.org/2004/07/">
  <VoidResult>
    <a:PosAmount>1500.0000000000</a:PosAmount>
    <a:PosAuthCode>974086</a:PosAuthCode>
    <a:PosCurrencyCode>784</a:PosCurrencyCode>
    <a:PosRRN>625969470006</a:PosRRN>
    <a:PosRespCode>00</a:PosRespCode>
    <a:PosRespText>Approved</a:PosRespText>
    <a:WebResponseStatus>Success</a:WebResponseStatus>
  </VoidResult>
</VoidResponse>"""


@tagged('post_install', '-at_install')
class TestAfsInvoiceFlow(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.currency_aed = cls.env.ref('base.AED')
        cls.currency_aed.active = True

        # Don't assume env.company has a chart of accounts configured (it may
        # not, on a database with several demo companies) — pick whichever
        # company actually has an income account, and work as that company.
        cls.income_account = cls.env['account.account'].search([('account_type', '=', 'income')], limit=1)
        if not cls.income_account:
            raise unittest.SkipTest('No company with an income account found on this database.')
        cls.company = cls.income_account.company_ids[:1]
        cls.env = cls.env(context=dict(cls.env.context, allowed_company_ids=cls.company.ids))
        cls.company.currency_id = cls.currency_aed

        cls.partner = cls.env['res.partner'].create({'name': 'AFS Test Customer'})

        cls.clearing_journal = cls.env['account.journal'].create({
            'name': 'AFS Card Clearing',
            'type': 'bank',
            'code': 'AFST',
            'company_id': cls.company.id,
            'currency_id': cls.currency_aed.id,
        })

        cls.env['ir.config_parameter'].sudo().set_param('afs_pos_test.secure_key', 'secret123')
        cls.terminal = cls.env['afs.terminal'].create({
            'name': 'AFS Test Terminal (unit tests)',
            # Deliberately not the spec's demo MID/TID (400000436830001 /
            # 40046947) — a real terminal configured from that same sample
            # would collide with the (mid, tid, company_id) unique constraint.
            'mid': 'TEST0000000MID',
            'tid': 'TEST0TID',
            'tenant': 'raalc-tenant',
            'secure_key_param': 'afs_pos_test.secure_key',
            'currency_id': cls.currency_aed.id,
            'journal_id': cls.clearing_journal.id,
            'company_id': cls.company.id,
        })

    def _create_invoice(self, amount=1500.0, move_type='out_invoice', reversed_entry_id=False):
        move = self.env['account.move'].create({
            'move_type': move_type,
            'partner_id': self.partner.id,
            'company_id': self.company.id,
            'currency_id': self.currency_aed.id,
            'reversed_entry_id': reversed_entry_id,
            'invoice_line_ids': [(0, 0, {
                'name': 'Test line',
                'quantity': 1,
                'price_unit': amount,
                'account_id': self.income_account.id,
            })],
        })
        move.action_post()
        return move

    def _make_wizard(self, move, amount=None):
        return self.env['afs.payment.wizard'].create({
            'move_id': move.id,
            'terminal_id': self.terminal.id,
            'amount': amount if amount is not None else move.amount_residual,
        })

    def _confirm(self, wizard):
        """Call action_confirm() with TransactionCase's cr.commit() guard
        lifted for the duration of the call — see the module docstring.
        Calls through to the REAL (unpatched) TestCursor.commit, which still
        flushes pending ORM writes and releases the test's savepoint, rather
        than a no-op that would leave writes unflushed and invisible to the
        wizard's own fresh-cursor reads."""
        real_commit = type(self.env.cr).commit
        with patch.object(self.env.cr, 'commit', lambda: real_commit(self.env.cr)):
            return wizard.action_confirm()

    def test_wizard_defaults_amount_to_residual(self):
        move = self._create_invoice(amount=1500.0)
        action = move.action_afs_pay()
        wizard_form = Form(self.env['afs.payment.wizard'].with_context(action['context']))
        self.assertEqual(wizard_form.amount, move.amount_residual)

    def test_approved_sale_creates_reconciled_payment(self):
        move = self._create_invoice(amount=1500.0)
        wizard = self._make_wizard(move)
        with patch.object(afs_client.AfsClient, 'post', return_value=APPROVED_XML.format(amount='1500.0000000000')):
            self._confirm(wizard)

        move.invalidate_recordset()
        self.assertEqual(move.afs_state, 'paid')
        sale_txn = move.afs_transaction_ids.filtered(lambda t: t.operation == 'sale')
        self.assertEqual(sale_txn.state, 'approved')
        self.assertTrue(sale_txn.payment_id)
        self.assertIn(move.payment_state, ('paid', 'in_payment'))
        self.assertEqual(move.afs_pos_invoice_number, '000006')

    def test_declined_sale_leaves_invoice_open(self):
        move = self._create_invoice(amount=1500.0)
        wizard = self._make_wizard(move)
        with patch.object(afs_client.AfsClient, 'post', return_value=DECLINED_XML):
            self._confirm(wizard)

        move.invalidate_recordset()
        self.assertEqual(move.afs_state, 'declined')
        sale_txn = move.afs_transaction_ids.filtered(lambda t: t.operation == 'sale')
        self.assertFalse(sale_txn.payment_id)
        self.assertEqual(move.payment_state, 'not_paid')

    def test_concurrent_attempt_refused(self):
        move = self._create_invoice(amount=1500.0)
        self.env['afs.transaction'].create({
            'move_id': move.id,
            'terminal_id': self.terminal.id,
            'company_id': self.company.id,
            'operation': 'sale',
            'state': 'pending',
            'requested_amount': 1500.0,
        })
        wizard = self._make_wizard(move)
        with self.assertRaises(UserError):
            self._confirm(wizard)

    def test_dropped_connection_recovers_via_enquiry_by_ref(self):
        # A dropped Sale must recover via EnquiryByRef keyed on our own
        # reference token, not a blank Enquiry — see afs_payment_wizard.py.
        move = self._create_invoice(amount=1500.0)
        wizard = self._make_wizard(move)

        def _side_effect(client_self, operation, envelope):
            if operation == 'Sale':
                raise requests.ConnectionError('connection dropped mid-sale')
            self.assertEqual(operation, 'EnquiryByRef')
            return APPROVED_XML.format(amount='1500.0000000000')

        with patch.object(afs_client.AfsClient, 'post', _side_effect):
            self._confirm(wizard)

        move.invalidate_recordset()
        sale_txn = move.afs_transaction_ids.filtered(lambda t: t.operation == 'sale')
        self.assertEqual(sale_txn.state, 'approved')
        self.assertTrue(sale_txn.payment_id)
        self.assertEqual(move.afs_state, 'paid')

    def test_full_void_uses_terminal_invoice_number(self):
        move = self._create_invoice(amount=1500.0)
        wizard = self._make_wizard(move)
        with patch.object(afs_client.AfsClient, 'post', return_value=APPROVED_XML.format(amount='1500.0000000000')):
            self._confirm(wizard)
        move.invalidate_recordset()
        sale_txn = move.afs_transaction_ids.filtered(lambda t: t.operation == 'sale')

        captured = {}

        def _fake_post(self, operation, envelope):
            captured['operation'] = operation
            captured['envelope'] = envelope
            return VOID_APPROVED_XML

        with patch.object(afs_client.AfsClient, 'post', _fake_post):
            void_txn = sale_txn.action_void()

        self.assertEqual(captured['operation'], 'Void')
        self.assertIn('<dc:OrigInvoiceNumber>000006</dc:OrigInvoiceNumber>', captured['envelope'])
        self.assertNotIn(sale_txn.our_reference, captured['envelope'])
        self.assertEqual(void_txn.state, 'approved')
        self.assertEqual(sale_txn.reversed_by_id, void_txn)
        move.invalidate_recordset()
        self.assertEqual(move.afs_state, 'reversed')

    def _create_credit_note(self, move, amount):
        return self.env['account.move'].create({
            'move_type': 'out_refund',
            'partner_id': self.partner.id,
            'company_id': self.company.id,
            'currency_id': self.currency_aed.id,
            'reversed_entry_id': move.id,
            'invoice_line_ids': [(0, 0, {
                'name': 'Refund',
                'quantity': 1,
                'price_unit': amount,
                'account_id': self.income_account.id,
            })],
        })

    def test_partial_credit_note_void_refused(self):
        # Auto-void-on-credit-note is on by default — no explicit opt-in needed.
        move = self._create_invoice(amount=1500.0)
        wizard = self._make_wizard(move)
        with patch.object(afs_client.AfsClient, 'post', return_value=APPROVED_XML.format(amount='1500.0000000000')):
            self._confirm(wizard)
        move.invalidate_recordset()

        credit_note = self._create_credit_note(move, 500.0)
        with self.assertRaises(UserError):
            credit_note.action_post()

    def test_full_credit_note_triggers_auto_void(self):
        move = self._create_invoice(amount=1500.0)
        wizard = self._make_wizard(move)
        with patch.object(afs_client.AfsClient, 'post', return_value=APPROVED_XML.format(amount='1500.0000000000')):
            self._confirm(wizard)
        move.invalidate_recordset()

        credit_note = self._create_credit_note(move, 1500.0)
        with patch.object(afs_client.AfsClient, 'post', return_value=VOID_APPROVED_XML):
            credit_note.action_post()

        move.invalidate_recordset()
        self.assertEqual(move.afs_state, 'reversed')

    def test_auto_void_setting_off_actually_sticks(self):
        # Regression guard: writing False through res.config.settings must not
        # be indistinguishable from "never configured" (ir.config_parameter
        # deletes the row on a plain False write) — see res_config_settings.py.
        settings = self.env['res.config.settings'].create({'afs_auto_void_on_credit_note': False})
        settings.execute()
        self.assertEqual(
            self.env['ir.config_parameter'].sudo().get_param('afs_pos_integration.auto_void_on_credit_note'),
            'false',
        )

        move = self._create_invoice(amount=1500.0)
        wizard = self._make_wizard(move)
        with patch.object(afs_client.AfsClient, 'post', return_value=APPROVED_XML.format(amount='1500.0000000000')):
            self._confirm(wizard)
        move.invalidate_recordset()

        credit_note = self._create_credit_note(move, 1500.0)
        credit_note.action_post()  # must NOT raise and must NOT void

        move.invalidate_recordset()
        self.assertEqual(move.afs_state, 'paid')

        # restore for any other test relying on the default
        self.env['ir.config_parameter'].sudo().set_param('afs_pos_integration.auto_void_on_credit_note', 'true')
