# -*- coding: utf-8 -*-
"""Invoice/wizard flow tests with the SOAP layer mocked.

NOTE ON CURSORS: the Sale/Void flows deliberately record their 'pending'
transaction on a separate cursor and commit it before calling the terminal
(see afs.transaction._create_pending_locked), so an orphaned record survives
a worker crash instead of rolling back invisibly. setUpClass puts the
registry in test mode, so those "separate" cursors are TestCursors sharing
the test's transaction and the background work runs inline instead of in a
thread — the flow is the real one, just synchronous, and everything is
rolled back at the end like any other TransactionCase.
"""
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import pytz
import requests

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import Form, TransactionCase, new_test_user, tagged

from ..services import afs_client

APPROVED_XML = """<?xml version="1.0"?>
<SaleResponse xmlns:a="http://schemas.datacontract.org/2004/07/">
  <SaleResult>
    <a:PosAmount>{amount}</a:PosAmount>
    <a:PosAuthCode>974086</a:PosAuthCode>
    <a:PosBatchNumber>001</a:PosBatchNumber>
    <a:PosCurrencyCode>784</a:PosCurrencyCode>
    <a:PosDate>{date}</a:PosDate>
    <a:PosEmvAppName>Visa DEBIT</a:PosEmvAppName>
    <a:PosInvoiceNumber>000006</a:PosInvoiceNumber>
    <a:PosIssuerName>VISA</a:PosIssuerName>
    <a:PosPan>{pan}</a:PosPan>
    <a:PosRRN>625969470006</a:PosRRN>
    <a:PosRespCode>00</a:PosRespCode>
    <a:PosRespStatus>1</a:PosRespStatus>
    <a:PosRespText>Approved</a:PosRespText>
    <a:PosStan>000006</a:PosStan>
    <a:PosTime>{time}</a:PosTime>
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

# What AFS really answers to the in-flight Sale once RequestCancellation
# has gone through (captured from the live endpoint).
CANCELLED_BY_ECR_XML = """<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>
<SaleResponse xmlns="http://tempuri.org/"><SaleResult xmlns:a="http://schemas.datacontract.org/2004/07/">
<a:PosAmount>0.0000000000</a:PosAmount><a:PosCVMId>0</a:PosCVMId><a:PosRespStatus>0</a:PosRespStatus>
<a:WebResponseErrorDesc>Cancelled By ECR</a:WebResponseErrorDesc><a:WebResponseStatus>Faild</a:WebResponseStatus>
</SaleResult></SaleResponse></s:Body></s:Envelope>"""

CANCELLATION_OK_XML = """<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>
<RequestCancellationResponse xmlns="http://tempuri.org/"><RequestCancellationResult xmlns:a="http://schemas.datacontract.org/2004/07/">
<a:WebResponseErrorDesc>Ecr Cancellation</a:WebResponseErrorDesc><a:WebResponseStatus>Success</a:WebResponseStatus>
</RequestCancellationResult></RequestCancellationResponse></s:Body></s:Envelope>"""

# The terminal's card prompt timed out (captured live, ~30 s after the prompt).
TIMEOUT_XML = ('<r><PosAmount>0.0000000000</PosAmount><PosRespStatus>0</PosRespStatus>'
               '<WebResponseErrorDesc>Transaction Timeout</WebResponseErrorDesc>'
               '<WebResponseStatus>Faild</WebResponseStatus></r>')
# EnquiryByRef for a reference AFS never processed.
NO_RECORD_XML = ('<r><PosAmount>0.0000000000</PosAmount><PosRespStatus>0</PosRespStatus>'
                 '<WebResponseStatus>Success</WebResponseStatus></r>')

DUBAI = pytz.timezone('Asia/Dubai')


def approved_xml(amount='1500.0000000000', local_dt=None, pan='408578******5322'):
    """Sale approval as the terminal sends it: date/time in the terminal's
    own local time (Asia/Dubai), defaulting to now."""
    local_dt = local_dt or datetime.now(DUBAI).replace(tzinfo=None)
    return APPROVED_XML.format(
        amount=amount, date=local_dt.strftime('%Y%m%d'), time=local_dt.strftime('%H%M%S'), pan=pan)


@tagged('post_install', '-at_install')
class TestAfsInvoiceFlow(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # TransactionCase (unlike HttpCase) does not put the registry in test
        # mode. Do it here: the flows' "fresh" cursors then become TestCursors
        # on this test's own transaction, and the background work runs inline.
        # Nothing is ever really committed, so the test database stays clean.
        cls.registry.enter_test_mode(cls.cr)
        cls.addClassCleanup(cls.registry.leave_test_mode)
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

        cls.env['ir.config_parameter'].sudo().set_param('afs_pos.secure_key.unit_test', 'secret123')
        cls.terminal = cls.env['afs.terminal'].create({
            'name': 'AFS Test Terminal (unit tests)',
            # Deliberately not the spec's demo MID/TID (400000436830001 /
            # 40046947) — a real terminal configured from that same sample
            # would collide with the (mid, tid, company_id) unique constraint.
            'mid': 'TEST0000000MID',
            'tid': 'TEST0TID',
            'tenant': 'raalc-tenant',
            'secure_key_param': 'afs_pos.secure_key.unit_test',
            'currency_id': cls.currency_aed.id,
            'journal_id': cls.clearing_journal.id,
            'company_id': cls.company.id,
            'tz': 'Asia/Dubai',
            # Most tests void straight after the sale; the delay itself is
            # covered by the test_void_delay_* tests.
            'void_delay_seconds': 0,
        })

        common = dict(company_id=cls.company.id, company_ids=[(6, 0, cls.company.ids)])
        cls.afs_user = new_test_user(
            cls.env, login='afs_cashier',
            groups='base.group_user,account.group_account_invoice,afs_pos_integration.group_afs_user', **common)
        cls.afs_manager = new_test_user(
            cls.env, login='afs_manager',
            groups='base.group_user,account.group_account_invoice,afs_pos_integration.group_afs_manager', **common)
        cls.plain_accountant = new_test_user(
            cls.env, login='afs_plain_accountant',
            groups='base.group_user,account.group_account_invoice', **common)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _create_invoice(self, amount=1500.0, move_type='out_invoice'):
        move = self.env['account.move'].create({
            'move_type': move_type,
            'partner_id': self.partner.id,
            'company_id': self.company.id,
            'currency_id': self.currency_aed.id,
            'invoice_line_ids': [(0, 0, {
                'name': 'Test line',
                'quantity': 1,
                'price_unit': amount,
                'account_id': self.income_account.id,
            })],
        })
        move.action_post()
        return move

    def _make_wizard(self, move, amount=None, terminal=None, user=None):
        env = self.env(user=user) if user else self.env
        return env['afs.payment.wizard'].create({
            'move_id': move.id,
            'terminal_id': (terminal or self.terminal).id,
            'amount': amount if amount is not None else move.amount_residual,
        })

    def _confirm(self, wizard):
        return wizard.action_confirm()

    def _pay(self, move, response=None):
        with patch.object(afs_client.AfsClient, 'post', return_value=response or approved_xml()):
            self._confirm(self._make_wizard(move))
        self.env.invalidate_all()
        return move.afs_transaction_ids.filtered(lambda t: t.operation == 'sale')

    def _create_credit_note(self, move, amount, void_on_post=True):
        return self.env['account.move'].create({
            'move_type': 'out_refund',
            'partner_id': self.partner.id,
            'company_id': self.company.id,
            'currency_id': self.currency_aed.id,
            'reversed_entry_id': move.id,
            'afs_void_on_post': void_on_post,
            'invoice_line_ids': [(0, 0, {
                'name': 'Refund',
                'quantity': 1,
                'price_unit': amount,
                'account_id': self.income_account.id,
            })],
        })

    # ------------------------------------------------------------------
    # Sale
    # ------------------------------------------------------------------

    def test_wizard_defaults_amount_to_residual(self):
        move = self._create_invoice(amount=1500.0)
        action = move.action_afs_pay()
        wizard_form = Form(self.env['afs.payment.wizard'].with_context(action['context']))
        self.assertEqual(wizard_form.amount, move.amount_residual)

    def test_approved_sale_creates_reconciled_payment(self):
        move = self._create_invoice(amount=1500.0)
        sale_txn = self._pay(move)

        self.assertEqual(move.afs_state, 'paid')
        self.assertEqual(sale_txn.state, 'approved')
        self.assertTrue(sale_txn.payment_id)
        self.assertTrue(sale_txn.finished_at, 'the terminal must be released once the call returns')
        self.assertIn(move.payment_state, ('paid', 'in_payment'))
        self.assertEqual(move.afs_pos_invoice_number, '000006')

    def test_declined_sale_leaves_invoice_open(self):
        move = self._create_invoice(amount=1500.0)
        sale_txn = self._pay(move, DECLINED_XML)

        self.assertEqual(move.afs_state, 'declined')
        self.assertFalse(sale_txn.payment_id)
        self.assertEqual(move.payment_state, 'not_paid')

    def test_terminal_cancellation_is_one_cancelled_record(self):
        move = self._create_invoice(amount=1500.0)
        self._pay(move, CANCELLED_BY_ECR_XML)
        self.assertEqual(len(move.afs_transaction_ids), 1)
        sale_txn = move.afs_transaction_ids
        self.assertEqual(sale_txn.state, 'cancelled')
        self.assertFalse(sale_txn.payment_id)
        self.assertEqual(move.afs_state, 'none')
        self.assertEqual(move.payment_state, 'not_paid')

    def test_cancel_button_keeps_single_record_until_terminal_answers(self):
        move = self._create_invoice(amount=1500.0)
        txn = self.env['afs.transaction'].create({
            'move_id': move.id,
            'terminal_id': self.terminal.id,
            'company_id': self.company.id,
            'operation': 'sale',
            'state': 'pending',
            'requested_amount': 1500.0,
            'our_reference': 'ABCD1234',
            'started_at': fields.Datetime.now(),
        })
        self.env.flush_all()
        with patch.object(afs_client.AfsClient, 'post', return_value=CANCELLATION_OK_XML):
            move.with_user(self.afs_user).action_afs_cancel_pending()
        self.env.invalidate_all()
        self.assertEqual(move.afs_transaction_ids, txn, 'Cancel must not create extra transaction rows')
        self.assertEqual(txn.state, 'pending')
        self.assertTrue(txn.cancel_requested_at)
        self.assertIn('Success', txn.cancel_response_status)

        with self.assertRaises(UserError):  # a second click is refused
            txn.action_request_cancellation()

        # ...then the in-flight Sale call returns AFS's cancellation answer.
        txn._finalize_sale(self.terminal._parse_response(CANCELLED_BY_ECR_XML))
        self.env.invalidate_all()
        self.assertEqual(move.afs_transaction_ids, txn)
        self.assertEqual(txn.state, 'cancelled')
        self.assertEqual(move.afs_state, 'none')

    def test_charge_that_beats_the_cancel_is_recorded_as_approved(self):
        move = self._create_invoice(amount=1500.0)
        txn = self.env['afs.transaction'].create({
            'move_id': move.id,
            'terminal_id': self.terminal.id,
            'company_id': self.company.id,
            'operation': 'sale',
            'state': 'pending',
            'requested_amount': 1500.0,
            'started_at': fields.Datetime.now(),
            'cancel_requested_at': fields.Datetime.now(),
        })
        self.env.flush_all()  # in production the pending record is committed long before the answer
        txn._finalize_sale(self.terminal._parse_response(approved_xml()))
        self.env.invalidate_all()
        self.assertEqual(move.afs_transaction_ids, txn)
        self.assertEqual(txn.state, 'approved')
        self.assertTrue(txn.payment_id)
        self.assertEqual(move.afs_state, 'paid')

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
        # reference token, not a blank Enquiry.
        move = self._create_invoice(amount=1500.0)
        wizard = self._make_wizard(move)

        def _side_effect(client_self, operation, envelope):
            if operation == 'Sale':
                raise requests.ConnectionError('connection dropped mid-sale')
            self.assertEqual(operation, 'EnquiryByRef')
            return approved_xml()

        with patch.object(afs_client.AfsClient, 'post', _side_effect):
            self._confirm(wizard)

        self.env.invalidate_all()
        sale_txn = move.afs_transaction_ids.filtered(lambda t: t.operation == 'sale')
        self.assertEqual(sale_txn.state, 'approved')
        self.assertTrue(sale_txn.payment_id)
        self.assertEqual(move.afs_state, 'paid')

    def test_gateway_5xx_recovers_via_enquiry_by_ref(self):
        # Seen live: AFS's gateway answered HTTP 524 (origin timeout) to a Sale.
        # The outcome is unknown, not "not charged": recover by reference.
        move = self._create_invoice(amount=1500.0)
        response = requests.Response()
        response.status_code = 524

        def _side_effect(client_self, operation, envelope):
            if operation == 'Sale':
                raise requests.HTTPError('524 Server Error', response=response)
            self.assertEqual(operation, 'EnquiryByRef')
            return approved_xml()

        with patch.object(afs_client.AfsClient, 'post', _side_effect):
            self._confirm(self._make_wizard(move))
        self.env.invalidate_all()
        sale_txn = move.afs_transaction_ids.filtered(lambda t: t.operation == 'sale')
        self.assertEqual(sale_txn.state, 'approved')
        self.assertTrue(sale_txn.payment_id)

    def test_gateway_5xx_with_no_afs_record_is_unknown_not_error(self):
        move = self._create_invoice(amount=1500.0)
        response = requests.Response()
        response.status_code = 524
        not_found = ('<r><PosAmount>0.0000000000</PosAmount><PosRespStatus>0</PosRespStatus>'
                     '<WebResponseStatus>Success</WebResponseStatus></r>')

        def _side_effect(client_self, operation, envelope):
            if operation == 'Sale':
                raise requests.HTTPError('524 Server Error', response=response)
            return not_found

        with patch.object(afs_client.AfsClient, 'post', _side_effect):
            self._confirm(self._make_wizard(move))
        self.env.invalidate_all()
        sale_txn = move.afs_transaction_ids.filtered(lambda t: t.operation == 'sale')
        self.assertEqual(sale_txn.state, 'unknown')
        self.assertFalse(sale_txn.payment_id)

    def _sale_with_responses(self, move, responses):
        calls = []

        def _side_effect(client_self, operation, envelope):
            calls.append(operation)
            answer = responses[operation]
            if isinstance(answer, Exception):
                raise answer
            return answer

        with patch.object(afs_client.AfsClient, 'post', _side_effect):
            self._confirm(self._make_wizard(move))
        self.env.invalidate_all()
        return move.afs_transaction_ids.filtered(lambda t: t.operation == 'sale'), calls

    def test_terminal_timeout_with_no_afs_record_is_cancelled(self):
        move = self._create_invoice(amount=1500.0)
        sale_txn, calls = self._sale_with_responses(
            move, {'Sale': TIMEOUT_XML, 'EnquiryByRef': NO_RECORD_XML})
        self.assertEqual(calls, ['Sale', 'EnquiryByRef'], 'the timeout must be confirmed with AFS first')
        self.assertEqual(len(sale_txn), 1)
        self.assertEqual(sale_txn.state, 'cancelled')
        self.assertEqual(sale_txn.web_response_error, 'Transaction Timeout')
        self.assertFalse(sale_txn.payment_id)
        self.assertEqual(move.afs_state, 'none', 'the invoice can be paid again without a manual Resolve')
        self.assertEqual(move.payment_state, 'not_paid')

    def test_terminal_timeout_but_afs_has_approval_registers_payment(self):
        move = self._create_invoice(amount=1500.0)
        sale_txn, _calls = self._sale_with_responses(
            move, {'Sale': TIMEOUT_XML, 'EnquiryByRef': approved_xml()})
        self.assertEqual(sale_txn.state, 'approved')
        self.assertTrue(sale_txn.payment_id)
        self.assertEqual(move.afs_state, 'paid')

    def test_terminal_timeout_with_failed_lookup_stays_unknown(self):
        move = self._create_invoice(amount=1500.0)
        sale_txn, _calls = self._sale_with_responses(
            move, {'Sale': TIMEOUT_XML, 'EnquiryByRef': requests.ConnectionError('lookup failed')})
        self.assertEqual(sale_txn.state, 'unknown')
        self.assertFalse(sale_txn.payment_id)

    def test_void_timeout_with_no_afs_record_is_cancelled_not_refunded(self):
        move = self._create_invoice(amount=1500.0)
        sale_txn = self._pay(move)
        payment = sale_txn.payment_id
        calls = []

        def _side_effect(client_self, operation, envelope):
            calls.append(operation)
            return TIMEOUT_XML if operation == 'Void' else NO_RECORD_XML

        with patch.object(afs_client.AfsClient, 'post', _side_effect):
            void_txn = sale_txn.with_user(self.afs_manager).action_void()
        self.env.invalidate_all()
        self.assertEqual(calls, ['Void', 'EnquiryByRef'])
        self.assertEqual(void_txn.state, 'cancelled')
        self.assertFalse(sale_txn.reversed_by_id)
        self.assertEqual(payment.state, 'paid', 'nothing was refunded, so the payment stays')
        self.assertEqual(move.afs_state, 'paid', 'the Void can simply be run again')

    def test_terminal_time_stored_as_utc(self):
        # C6: the terminal reports Dubai local time; Odoo must store UTC.
        move = self._create_invoice(amount=1500.0)
        local = datetime.now(DUBAI).replace(tzinfo=None, microsecond=0)
        sale_txn = self._pay(move, approved_xml(local_dt=local))
        self.assertEqual(sale_txn.pos_datetime, local - timedelta(hours=4))

    def test_unmasked_pan_is_masked_and_payment_still_registered(self):
        # H5: a terminal returning a full PAN must not leave a charged card
        # with no linked transaction; the PAN is masked before storage.
        move = self._create_invoice(amount=1500.0)
        sale_txn = self._pay(move, approved_xml(pan='4111111111111111'))
        self.assertEqual(sale_txn.state, 'approved')
        self.assertTrue(sale_txn.payment_id)
        self.assertTrue(afs_client.is_masked_pan(sale_txn.pos_pan))
        self.assertNotIn('4111111111111111', sale_txn.response_xml)

    def test_payment_posting_failure_records_single_sale(self):
        # H1: a payment-posting failure must not create a second, duplicate
        # 'approved' sale; the one record keeps the auth code AND the error.
        move = self._create_invoice(amount=1500.0)
        register_cls = self.registry['account.payment.register']
        with patch.object(register_cls, '_create_payments', side_effect=UserError('posting boom')):
            sale_txn = self._pay(move)
        self.assertEqual(len(sale_txn), 1)
        self.assertEqual(sale_txn.state, 'approved')
        self.assertEqual(sale_txn.pos_auth_code, '974086')
        self.assertIn('posting boom', sale_txn.error_message)
        self.assertFalse(sale_txn.payment_id)

    # ------------------------------------------------------------------
    # Terminal availability (C5)
    # ------------------------------------------------------------------

    def test_terminal_busy_blocks_other_invoice(self):
        other = self._create_invoice(amount=200.0)
        self.env['afs.transaction'].create({
            'move_id': other.id,
            'terminal_id': self.terminal.id,
            'company_id': self.company.id,
            'operation': 'sale',
            'state': 'pending',
            'requested_amount': 200.0,
            'started_at': fields.Datetime.now(),
        })
        move = self._create_invoice(amount=1500.0)
        with self.assertRaisesRegex(UserError, 'busy'):
            self._confirm(self._make_wizard(move))

    def test_same_tid_on_another_terminal_record_counts_as_busy(self):
        # Live config: two terminal records (one per company) on TID 40046947.
        # AFS: one TID = one ECR; the device runs one request at a time.
        twin = self.terminal.copy({'name': 'Same device, other record', 'mid': 'OTHERMID'})
        other = self._create_invoice(amount=200.0)
        self.env['afs.transaction'].create({
            'move_id': other.id, 'terminal_id': twin.id, 'company_id': self.company.id,
            'operation': 'sale', 'state': 'pending', 'requested_amount': 200.0,
            'started_at': fields.Datetime.now(),
        })
        self.assertTrue(self.terminal._is_busy())
        move = self._create_invoice(amount=1500.0)
        with self.assertRaisesRegex(UserError, 'busy'):
            self._confirm(self._make_wizard(move))

    def test_settlement_success_with_only_web_status_is_approved(self):
        # Spec §4 sample: a successful Settlement answers just WebResponseStatus=Success.
        ok = ('<r><WebResponseStatus>Success</WebResponseStatus><WebResponseErrorDesc/></r>')
        with patch.object(afs_client.AfsClient, 'post', return_value=ok):
            txn = self.terminal.with_user(self.afs_manager).action_run_settlement()
        self.env.invalidate_all()
        self.assertEqual(txn.state, 'approved')
        self.assertTrue(self.terminal.last_settlement_date)

    def test_terminal_free_after_previous_sale_finished(self):
        self._pay(self._create_invoice(amount=100.0), approved_xml(amount='100.0000000000'))
        second = self._create_invoice(amount=1500.0)
        self.assertEqual(self._pay(second).state, 'approved')

    def test_stale_in_flight_record_does_not_block_forever(self):
        other = self._create_invoice(amount=200.0)
        self.env['afs.transaction'].create({
            'move_id': other.id,
            'terminal_id': self.terminal.id,
            'company_id': self.company.id,
            'operation': 'sale',
            'state': 'unknown',
            'requested_amount': 200.0,
            'started_at': fields.Datetime.now() - timedelta(hours=1),
        })
        self.assertFalse(self.terminal._is_busy())

    # ------------------------------------------------------------------
    # Wizard validation (H4)
    # ------------------------------------------------------------------

    def test_wizard_refuses_vendor_bill(self):
        bill = self.env['account.move'].create({
            'move_type': 'in_invoice',
            'partner_id': self.partner.id,
            'company_id': self.company.id,
            'currency_id': self.currency_aed.id,
            'invoice_date': fields.Date.today(),
            'invoice_line_ids': [(0, 0, {'name': 'x', 'quantity': 1, 'price_unit': 50.0})],
        })
        bill.action_post()
        wizard = self._make_wizard(bill, amount=50.0)
        with self.assertRaises(UserError):
            self._confirm(wizard)

    def test_wizard_enforces_terminal_user_restriction(self):
        self.terminal.user_ids = self.afs_manager
        move = self._create_invoice(amount=1500.0)
        wizard = self._make_wizard(move, user=self.afs_user)
        with self.assertRaises(AccessError):
            wizard.action_confirm()

    # ------------------------------------------------------------------
    # Access rights (C3, C4) and credential protection (C2)
    # ------------------------------------------------------------------

    def test_cashier_cannot_void_over_rpc(self):
        sale_txn = self._pay(self._create_invoice(amount=1500.0))
        with self.assertRaises(AccessError):
            sale_txn.with_user(self.afs_user).action_void()

    def test_cashier_cannot_run_settlement(self):
        with self.assertRaises(AccessError):
            self.terminal.with_user(self.afs_user).action_run_settlement()

    def test_users_cannot_forge_transactions(self):
        move = self._create_invoice(amount=1500.0)
        for user in (self.afs_user, self.afs_manager):
            with self.assertRaises(AccessError):
                self.env['afs.transaction'].with_user(user).create({
                    'move_id': move.id,
                    'terminal_id': self.terminal.id,
                    'operation': 'sale',
                    'state': 'approved',
                })
        sale_txn = self._pay(move)
        with self.assertRaises(AccessError):
            sale_txn.with_user(self.afs_manager).write({'reversed_by_id': False})

    def test_transactions_cannot_be_deleted(self):
        sale_txn = self._pay(self._create_invoice(amount=1500.0))
        with self.assertRaises(UserError):
            sale_txn.unlink()

    def test_secure_key_param_must_use_afs_prefix(self):
        with self.assertRaises(ValidationError):
            self.terminal.secure_key_param = 'database.secret'

    def test_endpoint_must_be_https_afs_host(self):
        with self.assertRaises(ValidationError):
            self.terminal.endpoint_url = 'http://ereceipt.afs.com.bh/x.svc'
        with self.assertRaises(ValidationError):
            self.terminal.endpoint_url = 'https://attacker.example.com/x.svc'

    def test_manager_cannot_change_endpoint_or_key_param(self):
        terminal = self.terminal.with_user(self.afs_manager)
        with self.assertRaises(AccessError):
            terminal.write({'endpoint_url': 'https://other.afs.com.bh/x.svc'})
        with self.assertRaises(AccessError):
            terminal.write({'secure_key_param': 'afs_pos.secure_key.other'})
        terminal.write({'max_amount': 5000.0})  # everything else stays editable

    def test_non_afs_accountant_can_create_and_post_invoices(self):
        move = self.env['account.move'].with_user(self.plain_accountant).create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'invoice_line_ids': [(0, 0, {'name': 'x', 'quantity': 1, 'price_unit': 10.0,
                                         'account_id': self.income_account.id})],
        })
        move.action_post()
        self.assertEqual(move.state, 'posted')

    # ------------------------------------------------------------------
    # Void
    # ------------------------------------------------------------------

    def test_full_void_uses_terminal_invoice_number_and_cancels_payment(self):
        move = self._create_invoice(amount=1500.0)
        sale_txn = self._pay(move)
        payment = sale_txn.payment_id

        captured = {}

        def _fake_post(client_self, operation, envelope):
            captured['operation'] = operation
            captured['envelope'] = envelope
            return VOID_APPROVED_XML

        with patch.object(afs_client.AfsClient, 'post', _fake_post):
            void_txn = sale_txn.with_user(self.afs_manager).action_void()

        self.env.invalidate_all()
        self.assertEqual(captured['operation'], 'Void')
        self.assertIn('<dc:OrigInvoiceNumber>000006</dc:OrigInvoiceNumber>', captured['envelope'])
        # Original terminal invoice number only (spec §3.2 / VoidByInvoice).
        self.assertIn('<dc:OrigAuthCode></dc:OrigAuthCode>', captured['envelope'])
        self.assertIn('<dc:OrigRrn></dc:OrigRrn>', captured['envelope'])
        self.assertNotIn(sale_txn.our_reference, captured['envelope'])
        self.assertEqual(void_txn.state, 'approved')
        self.assertEqual(sale_txn.reversed_by_id, void_txn)
        self.assertEqual(move.afs_state, 'reversed')
        # C1: Odoo 18 payment states — the payment must really be cancelled.
        self.assertEqual(payment.state, 'canceled')
        self.assertEqual(move.payment_state, 'not_paid')

    def test_void_delay_blocks_void_right_after_sale(self):
        move = self._create_invoice(amount=1500.0)
        sale_txn = self._pay(move)
        self.terminal.void_delay_seconds = 120

        def _no_call(*args, **kwargs):
            raise AssertionError('no request may reach AFS inside the void delay')

        with patch.object(afs_client.AfsClient, 'post', _no_call):
            with self.assertRaisesRegex(UserError, 'wait'):
                sale_txn.with_user(self.afs_manager).action_void()
            with self.assertRaisesRegex(UserError, 'wait'):
                move.with_user(self.afs_manager).action_afs_reverse()
        self.assertFalse(self.env['afs.transaction'].search_count(
            [('reverses_id', '=', sale_txn.id)]), 'no Void record may be created')

    def test_void_delay_allows_void_once_elapsed(self):
        move = self._create_invoice(amount=1500.0)
        sale_txn = self._pay(move)
        self.terminal.void_delay_seconds = 120
        later = sale_txn.finished_at + timedelta(seconds=121)
        with patch.object(fields.Datetime, 'now', lambda *a, **k: later), \
                patch.object(afs_client.AfsClient, 'post', lambda *a, **k: VOID_APPROVED_XML):
            void_txn = sale_txn.with_user(self.afs_manager).action_void()
        self.env.invalidate_all()
        self.assertEqual(void_txn.state, 'approved')
        self.assertEqual(move.afs_state, 'reversed')

    def test_complete_reversal_recovers_lost_bookkeeping(self):
        # What happened live on INV/2026/00015: AFS approved the Void, but the
        # thread's follow-up write was lost — sale not marked reversed,
        # payment still paid, credit note left open.
        move = self._create_invoice(amount=1500.0)
        sale_txn = self._pay(move)
        payment = sale_txn.payment_id
        credit_note = self._create_credit_note(move, 1500.0, void_on_post=False)
        credit_note.action_post()
        void_txn = self.env['afs.transaction'].create({
            'move_id': move.id,
            'credit_note_id': credit_note.id,
            'terminal_id': self.terminal.id,
            'company_id': self.company.id,
            'operation': 'void',
            'reverses_id': sale_txn.id,
            'state': 'approved',
            'pos_auth_code': '974086',
            'pos_rrn': '625969470006',
        })
        self.assertTrue(void_txn.reversal_incomplete)
        self.assertEqual(move.afs_state, 'paid')

        with self.assertRaises(AccessError):
            void_txn.with_user(self.afs_user).action_complete_reversal()
        void_txn.with_user(self.afs_manager).action_complete_reversal()

        self.env.invalidate_all()
        self.assertEqual(sale_txn.reversed_by_id, void_txn)
        self.assertEqual(payment.state, 'canceled')
        self.assertEqual(move.afs_state, 'reversed')
        self.assertEqual(move.payment_state, 'reversed')
        self.assertFalse(void_txn.reversal_incomplete)

    def _gateway_timeout(self):
        response = requests.Response()
        response.status_code = 524
        return requests.HTTPError('524 Server Error', response=response)

    def test_void_lost_by_gateway_recovers_via_enquiry_by_ref(self):
        move = self._create_invoice(amount=1500.0)
        sale_txn = self._pay(move)
        void_found = VOID_APPROVED_XML.replace('<a:PosRespCode>', '<a:PosTxnName>VOID</a:PosTxnName><a:PosRespCode>')

        def _side_effect(client_self, operation, envelope):
            if operation == 'Void':
                raise self._gateway_timeout()
            self.assertEqual(operation, 'EnquiryByRef')
            return void_found

        with patch.object(afs_client.AfsClient, 'post', _side_effect):
            void_txn = sale_txn.action_void()
        self.env.invalidate_all()
        self.assertEqual(void_txn.state, 'approved')
        self.assertEqual(sale_txn.reversed_by_id, void_txn)
        self.assertEqual(move.afs_state, 'reversed')

    def test_void_lost_and_unknown_to_afs_then_resolved_by_manager(self):
        # Exactly live Void #94: gateway 524, EnquiryByRef finds nothing.
        move = self._create_invoice(amount=1500.0)
        sale_txn = self._pay(move)
        payment = sale_txn.payment_id
        credit_note = self._create_credit_note(move, 1500.0)
        not_found = ('<r><PosAmount>0.0000000000</PosAmount><PosRespStatus>0</PosRespStatus>'
                     '<WebResponseStatus>Success</WebResponseStatus></r>')

        def _side_effect(client_self, operation, envelope):
            if operation == 'Void':
                raise self._gateway_timeout()
            return not_found

        with patch.object(afs_client.AfsClient, 'post', _side_effect):
            credit_note.action_post()
        self.env.invalidate_all()
        void_txn = sale_txn.search([('reverses_id', '=', sale_txn.id), ('operation', '=', 'void')])
        self.assertEqual(void_txn.state, 'unknown')
        self.assertEqual(move.afs_state, 'paid')  # nothing assumed

        wizard = self.env['afs.resolve.wizard'].with_user(self.afs_manager).with_context(
            active_model='afs.transaction', active_id=void_txn.id).create({
                'outcome': 'approved', 'pos_auth_code': '123456', 'pos_rrn': '628269470099',
                'note': 'Void receipt printed on the terminal'})
        with self.assertRaises(AccessError):
            wizard.with_user(self.afs_user).action_resolve()
        wizard.action_resolve()

        self.env.invalidate_all()
        self.assertEqual(void_txn.state, 'approved')
        self.assertIn('Void receipt printed', void_txn.error_message)
        self.assertEqual(sale_txn.reversed_by_id, void_txn)
        self.assertEqual(payment.state, 'canceled')
        self.assertEqual(move.afs_state, 'reversed')
        self.assertEqual(move.payment_state, 'reversed')

    def test_already_voided_answer_settles_the_lost_void(self):
        # Live sequence on INV/2026/00017: Void #94 lost (524, no AFS record),
        # re-sent Void #95 refused in 4 s with "Transaction has already been voided".
        move = self._create_invoice(amount=1500.0)
        sale_txn = self._pay(move)
        payment = sale_txn.payment_id
        credit_note = self._create_credit_note(move, 1500.0)
        not_found = ('<r><PosAmount>0.0000000000</PosAmount><PosRespStatus>0</PosRespStatus>'
                     '<WebResponseStatus>Success</WebResponseStatus></r>')

        def _lost(client_self, operation, envelope):
            if operation == 'Void':
                raise self._gateway_timeout()
            return not_found

        with patch.object(afs_client.AfsClient, 'post', _lost):
            credit_note.action_post()
        self.env.invalidate_all()
        lost_void = sale_txn.search([('reverses_id', '=', sale_txn.id), ('operation', '=', 'void')])
        self.assertEqual(lost_void.state, 'unknown')

        already = ('<r><PosAmount>0.0000000000</PosAmount><PosRespCode>uc</PosRespCode><PosRespStatus>0</PosRespStatus>'
                   '<PosRespText>Transaction has already been voided</PosRespText>'
                   '<WebResponseStatus>Success</WebResponseStatus></r>')
        with patch.object(afs_client.AfsClient, 'post', return_value=already):
            refused = sale_txn.action_void()
        self.env.invalidate_all()
        self.assertEqual(refused.state, 'error')
        self.assertEqual(lost_void.state, 'approved')
        self.assertIn('already been voided', lost_void.error_message)
        self.assertEqual(sale_txn.reversed_by_id, lost_void)
        self.assertEqual(payment.state, 'canceled')
        self.assertEqual(move.afs_state, 'reversed')
        self.assertEqual(move.payment_state, 'reversed')

    def test_unknown_sale_resolved_as_approved_registers_payment(self):
        move = self._create_invoice(amount=1500.0)
        txn = self.env['afs.transaction'].create({
            'move_id': move.id, 'terminal_id': self.terminal.id, 'company_id': self.company.id,
            'operation': 'sale', 'state': 'unknown', 'requested_amount': 1500.0,
        })
        self.env['afs.resolve.wizard'].with_user(self.afs_manager).with_context(
            active_model='afs.transaction', active_id=txn.id).create({
                'outcome': 'approved', 'pos_auth_code': '111111', 'pos_rrn': '222222222222',
                'note': 'AFS portal shows it approved'}).action_resolve()
        self.env.invalidate_all()
        self.assertEqual(txn.state, 'approved')
        self.assertTrue(txn.payment_id)
        self.assertEqual(move.afs_state, 'paid')
        self.assertIn(move.payment_state, ('paid', 'in_payment'))

    def test_unknown_sale_resolved_as_not_processed(self):
        move = self._create_invoice(amount=1500.0)
        txn = self.env['afs.transaction'].create({
            'move_id': move.id, 'terminal_id': self.terminal.id, 'company_id': self.company.id,
            'operation': 'sale', 'state': 'unknown', 'requested_amount': 1500.0,
        })
        self.env['afs.resolve.wizard'].with_user(self.afs_manager).with_context(
            active_model='afs.transaction', active_id=txn.id).create({
                'outcome': 'not_processed', 'note': 'No record on AFS portal'}).action_resolve()
        self.env.invalidate_all()
        self.assertEqual(txn.state, 'error')
        self.assertFalse(txn.payment_id)
        self.assertEqual(move.payment_state, 'not_paid')
        self.assertEqual(move.afs_state, 'none')

    def test_void_refused_after_settlement_with_timezone(self):
        # C6: sale at 11:00 Dubai = 07:00 UTC; settlement at 08:00 UTC is
        # AFTER the sale, so Void must be refused (the old local-as-UTC bug
        # compared 11:00 > 08:00 and allowed it).
        local = datetime.now(DUBAI).replace(tzinfo=None, microsecond=0)
        sale_txn = self._pay(self._create_invoice(amount=1500.0), approved_xml(local_dt=local))
        self.terminal.last_settlement_date = local - timedelta(hours=3)
        with self.assertRaisesRegex(UserError, 'settled'):
            sale_txn.action_void()

    # ------------------------------------------------------------------
    # Credit notes (H2, H3)
    # ------------------------------------------------------------------

    def test_flagged_partial_credit_note_refused(self):
        move = self._create_invoice(amount=1500.0)
        self._pay(move)
        credit_note = self._create_credit_note(move, 500.0)
        with self.assertRaises(UserError):
            credit_note.action_post()

    def test_unflagged_partial_credit_note_posts_without_void(self):
        move = self._create_invoice(amount=1500.0)
        self._pay(move)
        credit_note = self._create_credit_note(move, 500.0, void_on_post=False)
        with patch.object(afs_client.AfsClient, 'post', side_effect=AssertionError('terminal must not be called')):
            credit_note.action_post()
        self.env.invalidate_all()
        self.assertEqual(credit_note.state, 'posted')
        self.assertEqual(move.afs_state, 'paid')

    def test_unflagged_full_credit_note_never_voids(self):
        move = self._create_invoice(amount=1500.0)
        self._pay(move)
        credit_note = self._create_credit_note(move, 1500.0, void_on_post=False)
        with patch.object(afs_client.AfsClient, 'post', side_effect=AssertionError('terminal must not be called')):
            credit_note.action_post()
        self.env.invalidate_all()
        self.assertEqual(move.afs_state, 'paid')

    def test_flagged_full_credit_note_voids_and_reconciles(self):
        move = self._create_invoice(amount=1500.0)
        sale_txn = self._pay(move)
        payment = sale_txn.payment_id

        credit_note = self._create_credit_note(move, 1500.0)
        with patch.object(afs_client.AfsClient, 'post', return_value=VOID_APPROVED_XML):
            credit_note.with_user(self.afs_manager).action_post()

        self.env.invalidate_all()
        self.assertEqual(move.afs_state, 'reversed')
        self.assertEqual(payment.state, 'canceled')
        void_txn = sale_txn.reversed_by_id
        self.assertEqual(void_txn.credit_note_id, credit_note)
        # H3: invoice and credit note settle each other — the invoice ends Reversed.
        self.assertEqual(move.payment_state, 'reversed')
        self.assertIn(credit_note.payment_state, ('paid', 'reversed'))
        self.assertFalse(void_txn.reversal_incomplete)

    def test_flagged_credit_note_requires_afs_manager(self):
        move = self._create_invoice(amount=1500.0)
        self._pay(move)
        credit_note = self._create_credit_note(move, 1500.0)
        with self.assertRaises(AccessError):
            credit_note.with_user(self.afs_user).action_post()

    def test_reverse_card_payment_button_flags_credit_note(self):
        move = self._create_invoice(amount=1500.0)
        self._pay(move)
        action = move.with_user(self.afs_manager).action_afs_reverse()
        reversal = self.env['account.move.reversal'].with_context(action['context']).create({
            'journal_id': move.journal_id.id,
            'reason': 'card refund',
        })
        result = reversal.refund_moves()
        credit_note = self.env['account.move'].browse(result['res_id'])
        self.assertTrue(credit_note.afs_void_on_post)

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
        self._pay(move)

        credit_note = self._create_credit_note(move, 1500.0)
        credit_note.action_post()  # must NOT raise and must NOT void

        self.env.invalidate_all()
        self.assertEqual(move.afs_state, 'paid')

        # restore for any other test relying on the default
        self.env['ir.config_parameter'].sudo().set_param('afs_pos_integration.auto_void_on_credit_note', 'true')
