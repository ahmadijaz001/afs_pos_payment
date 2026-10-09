# -*- coding: utf-8 -*-
import logging
import threading
import uuid

import psycopg2
import psycopg2.errors
import requests

import odoo
from odoo import api, fields, models, _
from odoo.exceptions import UserError

from ..services import afs_client

_logger = logging.getLogger(__name__)


class AfsPaymentWizard(models.TransientModel):
    _name = 'afs.payment.wizard'
    _description = 'Take a Card Payment via AFS'

    move_id = fields.Many2one('account.move', string='Invoice', required=True)
    terminal_id = fields.Many2one('afs.terminal', string='Terminal', required=True)
    currency_id = fields.Many2one(related='move_id.currency_id', readonly=True)
    amount = fields.Monetary(currency_field='currency_id', required=True)

    @api.onchange('move_id')
    def _onchange_move_id(self):
        if self.move_id:
            self.amount = self.move_id.amount_residual

    @api.constrains('amount')
    def _check_amount(self):
        for wizard in self:
            if wizard.amount <= 0:
                raise UserError(_('The amount to charge must be greater than zero.'))
            if wizard.move_id and wizard.amount > wizard.move_id.amount_residual:
                raise UserError(_('The amount to charge cannot exceed the invoice residual (%s).')
                                 % wizard.move_id.amount_residual)
            if wizard.terminal_id and wizard.terminal_id.max_amount and wizard.amount > wizard.terminal_id.max_amount:
                raise UserError(_('The amount exceeds this terminal\'s per-transaction limit (%s).')
                                 % wizard.terminal_id.max_amount)

    def _check_currency(self):
        self.ensure_one()
        if self.move_id.currency_id != self.terminal_id.currency_id:
            raise UserError(_(
                'This invoice is in %s but terminal "%s" is configured for %s. '
                'Never charge a card in a currency the terminal was not set up for.'
            ) % (self.move_id.currency_id.name, self.terminal_id.name, self.terminal_id.currency_id.name))

    def action_confirm(self):
        """Starts the Sale and returns immediately — see the background-thread
        note on _start_sale_async for why this is no longer a single blocking
        call. The invoice reopens showing afs_state='pending'; the actual
        terminal wait, and the eventual approved/declined/unknown outcome,
        happen in a background thread and are pushed to the client over the
        bus rather than held open across one HTTP request."""
        self.ensure_one()
        move = self.move_id
        terminal = self.terminal_id
        self._check_currency()

        # 1. Row lock on the invoice so two users cannot charge it at once.
        try:
            self.env.cr.execute('SELECT id FROM account_move WHERE id = %s FOR UPDATE NOWAIT', (move.id,))
        except psycopg2.errors.LockNotAvailable:
            self.env.cr.rollback()
            raise UserError(_('A card payment is already in progress for this invoice.'))

        # 2. Refuse if another attempt on this invoice is still pending.
        if self.env['afs.transaction'].search_count([('move_id', '=', move.id), ('state', '=', 'pending')]):
            raise UserError(_('A card payment is already pending for this invoice.'))

        # Advisory lock on the terminal: shared with Void (models/afs_transaction.py)
        # since the physical device can't run two operations at once regardless of
        # which Odoo flow started it. Held until the background work finishes, not
        # released here — see _run_sale_background's finally block.
        lock_key = afs_client.TERMINAL_ADVISORY_LOCK_OFFSET + terminal.id
        self.env.cr.execute('SELECT pg_try_advisory_lock(%s)', (lock_key,))
        if not self.env.cr.fetchone()[0]:
            raise UserError(_('Terminal "%s" is currently busy with another transaction.') % terminal.name)

        try:
            self._start_sale_async(move, terminal, lock_key)
        except Exception:
            self.env.cr.execute('SELECT pg_advisory_unlock(%s)', (lock_key,))
            raise
        return self._reopen_invoice_action(move.id)

    def _start_sale_async(self, move, terminal, lock_key):
        token = uuid.uuid4().hex[:8].upper()
        config = terminal._get_config()
        printer = afs_client.AfsPrinter(
            invoice_number=token,
            reference_number=token,
            enable_print_pos_receipt=int(terminal.print_pos_receipt),
            enable_print_receipt_note=int(terminal.print_receipt_note),
            printer_width=terminal.printer_width,
        )
        request_xml = afs_client.build_sale_envelope(config, self.amount, printer)
        sale_started_at = fields.Datetime.now()

        # Persist BEFORE the blocking call and commit now: if the background
        # thread dies (worker restart, crash), an uncommitted record would
        # vanish, leaving a charged client with no trace. Worst case with the
        # commit is an orphaned 'pending' record the safety-net cron (or a
        # human) can resolve. Do not remove this commit as "unnecessary ORM code".
        txn = self.env['afs.transaction'].create({
            'move_id': move.id,
            'terminal_id': terminal.id,
            'company_id': move.company_id.id,
            'operation': 'sale',
            'requested_amount': self.amount,
            'currency_id': move.currency_id.id,
            'our_reference': token,
            'request_xml': request_xml,
            'state': 'pending',
            'started_at': sale_started_at,
        })
        self.env.cr.commit()

        args = (self.env.cr.dbname, self.env.uid, dict(self.env.context),
                self.id, txn.id, token, sale_started_at, lock_key)
        if self.env.registry.in_test_mode():
            # No real OS thread under TransactionCase: Registry(dbname).cursor()
            # would hand back a TestCursor wrapping the SAME underlying
            # connection as the test's own cursor, and using that from two
            # concurrent threads is a real corruption risk, not just a test
            # inconvenience. Run inline instead — same code path, same
            # outcome, just synchronous.
            self._run_sale_background(*args)
        else:
            threading.Thread(target=self._run_sale_background, args=args, daemon=True).start()

    def _run_sale_background(self, dbname, uid, context, wizard_id, txn_id, token, sale_started_at, lock_key):
        """Runs off-thread (see _start_sale_async): must not touch self.env —
        that cursor belongs to the request that already returned. Everything
        here works through its own fresh environment instead."""
        move_id = None
        registry = odoo.modules.registry.Registry(dbname)
        try:
            with registry.cursor() as cr:
                env = api.Environment(cr, uid, context)
                wizard = env['afs.payment.wizard'].browse(wizard_id)
                terminal = wizard.terminal_id
                move_id = wizard.move_id.id
                config = terminal._get_config()
                printer = afs_client.AfsPrinter(
                    invoice_number=token,
                    reference_number=token,
                    enable_print_pos_receipt=int(terminal.print_pos_receipt),
                    enable_print_receipt_note=int(terminal.print_receipt_note),
                    printer_width=terminal.printer_width,
                )
                request_xml = afs_client.build_sale_envelope(config, wizard.amount, printer)
                client = terminal._get_client()
                try:
                    response_xml = client.post('Sale', request_xml)
                except (requests.Timeout, requests.ConnectionError) as exc:
                    wizard._handle_dropped_connection(txn_id, terminal, config, token, sale_started_at, exc)
                except Exception as exc:  # noqa: BLE001 - unexpected transport failure
                    wizard._write_result(txn_id, move_id, terminal.id, {'state': 'error', 'error_message': str(exc)})
                else:
                    result = afs_client.parse_response(response_xml)
                    wizard._finalize_sale(txn_id, move_id, terminal.id, result, response_xml)
        except Exception:  # noqa: BLE001 - never let a background thread crash silently
            _logger.exception('AFS background Sale thread failed for afs.transaction %s', txn_id)
        finally:
            try:
                with registry.cursor() as cr:
                    cr.execute('SELECT pg_advisory_unlock(%s)', (lock_key,))
                    if move_id:
                        env = api.Environment(cr, uid, context)
                        env['bus.bus']._sendone(
                            env.user.partner_id, 'afs_pos_integration/payment_update', {'move_id': move_id})
                    cr.commit()
            except Exception:  # noqa: BLE001
                _logger.exception('Failed to release AFS advisory lock %s / notify for txn %s', lock_key, txn_id)

    def _finalize_sale(self, txn_id, move_id, terminal_id, result, response_xml):
        """Runs off-thread (called from _run_sale_background) — nothing here
        returns a client action any more; the bus notification sent by the
        caller's finally block is what tells the browser to refresh."""
        response_vals = _result_to_vals(result)
        response_vals['response_xml'] = response_xml

        if result.approved:
            state = 'approved'
        elif result.pos_resp_code:
            state = 'declined'
        else:
            state = 'unknown'
        response_vals['state'] = state

        # Register the payment whenever AFS says approved — regardless of what
        # happens next to our own bookkeeping row. If the card was charged,
        # that must never depend on winning a race against a Cancel click.
        if result.approved:
            payment = self._register_payment_fresh_cursor(txn_id, move_id, terminal_id, result)
            if payment:
                response_vals['payment_id'] = payment

        wrote = self._write_result(
            txn_id, move_id, terminal_id, response_vals,
            late_reason=_('the original Sale attempt was already cancelled/finalized before this response arrived'),
        )
        if wrote:
            if state == 'declined':
                self._post_chatter(move_id, _('Card declined: %s (%s)') % (result.pos_resp_text, result.pos_resp_code))
            elif state == 'unknown':
                self._post_chatter(move_id, _(
                    'AFS response could not be classified as approved or declined. '
                    'Outcome unknown — do not retry. Check the terminal / AFS portal.'
                ))
        elif state == 'approved':
            self._post_chatter(move_id, _(
                'Card was charged (auth %s / RRN %s) AFTER this Sale attempt had already been '
                'cancelled — the cancellation did not stop the terminal in time. Payment has been '
                'registered on a new, separate transaction. Please verify on the terminal/AFS '
                'portal that the card was not charged twice.'
            ) % (result.pos_auth_code, result.pos_rrn))

    def _handle_dropped_connection(self, txn_id, terminal, config, token, sale_started_at, exc):
        """EnquiryByRef is keyed by the reference WE generated and sent as this
        Sale's Printer/InvoiceNumber (spec §7: "based on original ECR reference
        number originated and sent by ECR"), so — unlike a blank Enquiry — this
        is a direct, unambiguous lookup of this exact attempt, not a guess at
        "whatever the terminal's last transaction was"."""
        _logger.warning('AFS Sale connection dropped for afs.transaction %s: %s', txn_id, exc)
        move_id = self.move_id.id
        recovery_printer = afs_client.AfsPrinter(invoice_number=token, reference_number=token)
        late_reason = _('a recovery attempt (EnquiryByRef) resolved after the original attempt was already finalized')
        try:
            client = terminal._get_client()
            _envelope, response_xml, result = client.enquiry_by_ref(
                config, orig_auth_code='', orig_invoice_number=token, orig_rrn='', printer=recovery_printer)
        except Exception as enquiry_exc:  # noqa: BLE001
            self._write_result(txn_id, move_id, terminal.id, {
                'state': 'unknown',
                'error_message': _(
                    'Connection dropped (%s) and recovery EnquiryByRef also failed: %s'
                ) % (exc, enquiry_exc),
            }, late_reason=late_reason)
            return

        # Something came back for our reference at all (vs. AFS having no record of it).
        found = bool(result.pos_rrn or result.pos_auth_code or result.pos_resp_code)

        if found and result.approved and afs_client.correlate_recovery(result, self.amount, sale_started_at):
            vals = _result_to_vals(result)
            vals['response_xml'] = response_xml
            vals['state'] = 'approved'
            payment = self._register_payment_fresh_cursor(txn_id, move_id, terminal.id, result)
            if payment:
                vals['payment_id'] = payment
            wrote = self._write_result(txn_id, move_id, terminal.id, vals, late_reason=late_reason)
            if not wrote:
                self._post_chatter(move_id, _(
                    'Card was charged (auth %s / RRN %s) — confirmed via recovery — AFTER this Sale '
                    'attempt had already been cancelled/finalized. Payment has been registered on a '
                    'new, separate transaction. Please verify the card was not charged twice.'
                ) % (result.pos_auth_code, result.pos_rrn))
            return

        if found and not result.approved:
            # Keyed lookup, not a guess: trust a declined outcome for our own reference.
            vals = _result_to_vals(result)
            vals['response_xml'] = response_xml
            vals['state'] = 'declined'
            if self._write_result(txn_id, move_id, terminal.id, vals, late_reason=late_reason):
                self._post_chatter(move_id, _(
                    'Card declined (recovered after a dropped connection): %s (%s)'
                ) % (result.pos_resp_text, result.pos_resp_code))
            return

        self._write_result(txn_id, move_id, terminal.id, {
            'state': 'unknown',
            'error_message': _(
                'Connection dropped during Sale (%s). Recovery EnquiryByRef for reference %s '
                'did not return a confirmed outcome. Outcome unknown — do not retry. Verify on '
                'the terminal or AFS portal before taking any action.'
            ) % (exc, token),
            'response_xml': response_xml,
        }, late_reason=late_reason)

    def _register_payment_fresh_cursor(self, txn_id, move_id, terminal_id, result):
        with self.env.registry.cursor() as new_cr:
            new_env = api.Environment(new_cr, self.env.uid, self.env.context)
            move = new_env['account.move'].browse(move_id)
            terminal = new_env['afs.terminal'].browse(terminal_id)
            amount = result.pos_amount if result.pos_amount is not None else self.amount
            if result.pos_amount is not None and result.pos_amount != self.amount:
                _logger.warning(
                    'AFS captured amount (%s) differs from requested amount (%s) for move %s; '
                    'using the captured amount.', result.pos_amount, self.amount, move_id)
            inbound_line = terminal.journal_id.inbound_payment_method_line_ids[:1]
            ctx = {'active_model': 'account.move', 'active_ids': move.ids, 'active_id': move.id}
            reg = new_env['account.payment.register'].with_context(**ctx).create({
                'amount': amount,
                'journal_id': terminal.journal_id.id,
                'payment_method_line_id': inbound_line.id if inbound_line else False,
                'payment_date': fields.Date.context_today(new_env['account.payment.register']),
                'communication': 'AFS %s/%s (term inv %s)' % (
                    result.pos_auth_code, result.pos_rrn, result.pos_invoice_number),
            })
            try:
                payments = reg._create_payments()
            except Exception as exc:  # noqa: BLE001
                new_cr.rollback()
                # Do NOT auto-void: the card has already been charged. A human
                # must reconcile this manually; an automatic reversal triggered
                # by a transient DB/posting error would be worse.
                self._write_result(txn_id, move_id, terminal_id, {
                    'state': 'approved',
                    'error_message': _(
                        'Card was charged (auth %s / RRN %s) but the Odoo payment could '
                        'not be posted: %s. Resolve manually — do not re-charge.'
                    ) % (result.pos_auth_code, result.pos_rrn, exc),
                }, late_reason=_('a payment-posting failure was being recorded after the transaction was already finalized'))
                self._post_chatter(move_id, _(
                    'Card charged successfully but payment registration failed: %s'
                ) % exc)
                return False
            new_cr.commit()
            return payments[:1].id if payments else False

    def _write_result(self, txn_id, move_id, terminal_id, vals, *, late_reason):
        """Race-safe write of a terminal result onto a 'pending' afs.transaction.

        Thin wrapper around afs.transaction._safe_finalize_by_id, which is
        shared with Void (models/afs_transaction.py) — see its docstring for
        the full rationale. move_id/terminal_id are accepted here only to
        keep call sites unchanged; the shared method reads them from the
        record itself.
        """
        return self.env['afs.transaction']._safe_finalize_by_id(txn_id, vals, late_reason=late_reason)

    def _post_chatter(self, move_id, body):
        with self.env.registry.cursor() as new_cr:
            new_env = api.Environment(new_cr, self.env.uid, self.env.context)
            new_env['account.move'].browse(move_id).message_post(body=body)
            new_cr.commit()

    def _reopen_invoice_action(self, move_id):
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'res_id': move_id,
            'view_mode': 'form',
            'target': 'current',
        }


def _result_to_vals(result: 'afs_client.AfsResult') -> dict:
    return {
        'web_response_status': result.web_response_status,
        'web_response_error': result.web_response_error,
        'pos_resp_code': result.pos_resp_code,
        'pos_resp_text': result.pos_resp_text,
        'pos_resp_status': result.pos_resp_status,
        'pos_amount': result.pos_amount or 0.0,
        'pos_currency_code': result.pos_currency_code,
        'pos_auth_code': result.pos_auth_code,
        'pos_rrn': result.pos_rrn,
        'pos_stan': result.pos_stan,
        'pos_invoice_number': result.pos_invoice_number,
        'pos_batch_number': result.pos_batch_number,
        'pos_pan': result.pos_pan,
        'pos_issuer_name': result.pos_issuer_name,
        'pos_scheme': result.pos_scheme,
        'pos_emv_aid': result.pos_emv_aid,
        'pos_emv_tvr': result.pos_emv_tvr,
        'pos_emv_cryptogram': result.pos_emv_cryptogram,
        'pos_entry_mode': result.pos_entry_mode,
        'pos_cvm_id': result.pos_cvm_id,
        'pos_txn_name': result.pos_txn_name,
        'pos_datetime': result.pos_datetime,
        'pos_receipt': result.pos_receipt,
    }
