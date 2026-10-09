# -*- coding: utf-8 -*-
import logging
import threading
import uuid
from datetime import timedelta

import odoo
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

from ..services import afs_client

_logger = logging.getLogger(__name__)

TERMINAL_STATES = ('approved', 'declined', 'unknown', 'error')
# Linkage fields the void/reversal workflow must be able to set even after a
# record has reached a terminal state — this is plumbing, not rewriting history.
_LINK_ONLY_FIELDS = {'reversed_by_id', 'payment_id'}


class AfsTransaction(models.Model):
    _name = 'afs.transaction'
    _description = 'AFS Terminal Transaction'
    _order = 'create_date desc'

    # Link
    move_id = fields.Many2one('account.move', string='Invoice', index=True, ondelete='restrict')
    terminal_id = fields.Many2one('afs.terminal', string='Terminal', required=True, ondelete='restrict')
    payment_id = fields.Many2one('account.payment', string='Payment', ondelete='set null')
    reverses_id = fields.Many2one('afs.transaction', string='Reverses', ondelete='restrict')
    reversed_by_id = fields.Many2one('afs.transaction', string='Reversed By', ondelete='set null')
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)

    # Request
    operation = fields.Selection([
        ('sale', 'Sale'),
        ('void', 'Void'),
        ('enquiry', 'Enquiry'),
        ('settlement', 'Settlement'),
        ('cancel', 'Request Cancellation'),
    ], required=True)
    requested_amount = fields.Monetary(currency_field='currency_id')
    currency_id = fields.Many2one('res.currency', default=lambda self: self.env.company.currency_id)
    our_reference = fields.Char(help='Short token sent as Printer/InvoiceNumber and ReferenceNumber. '
                                      'Not echoed back by the terminal — kept for our own logs only.')
    request_xml = fields.Text(string='Request XML (redacted)')

    # State
    state = fields.Selection([
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('declined', 'Declined'),
        ('unknown', 'Unknown'),
        ('error', 'Error'),
    ], required=True, default='pending', index=True)
    error_message = fields.Text()
    started_at = fields.Datetime(
        help='When the background Sale call actually started — used by the '
             'stale-pending safety-net cron, not by the request/response flow itself.'
    )

    # Response
    pos_amount = fields.Monetary(currency_field='currency_id')
    pos_currency_code = fields.Char(string='Currency Code (numeric)')
    pos_auth_code = fields.Char()
    pos_rrn = fields.Char(string='RRN')
    pos_stan = fields.Char(string='STAN')
    pos_invoice_number = fields.Char(string='Terminal Invoice Number')
    pos_batch_number = fields.Char()
    pos_resp_code = fields.Char()
    pos_resp_text = fields.Char()
    pos_resp_status = fields.Char(help='-1 unknown / 0 declined / 1 approved — one of two signals used to gate approval, alongside PosRespCode.')
    pos_pan = fields.Char(string='Masked PAN')
    pos_issuer_name = fields.Char()
    pos_scheme = fields.Char()
    pos_emv_aid = fields.Char(string='EMV AID')
    pos_emv_tvr = fields.Char(string='EMV TVR')
    pos_emv_cryptogram = fields.Char(string='EMV Cryptogram')
    pos_entry_mode = fields.Char()
    pos_cvm_id = fields.Char(string='CVM Id')
    pos_txn_name = fields.Char(string='Transaction Name')
    pos_datetime = fields.Datetime(string='Terminal Date/Time')
    pos_receipt = fields.Text()
    web_response_status = fields.Char()
    web_response_error = fields.Text()
    response_xml = fields.Text()

    @api.depends('operation', 'pos_invoice_number')
    def _compute_display_name(self):
        operation_labels = dict(self._fields['operation'].selection)
        for rec in self:
            label = f"{operation_labels.get(rec.operation, rec.operation)} #{rec.id}"
            if rec.pos_invoice_number:
                label += f" ({rec.pos_invoice_number})"
            rec.display_name = label

    @api.constrains('pos_pan')
    def _check_pan_masked(self):
        for rec in self:
            if rec.pos_pan and not afs_client.is_masked_pan(rec.pos_pan):
                raise ValidationError(_(
                    'The card number returned by the terminal does not look like a '
                    'masked PAN. Refusing to store it — this would be a PCI incident, '
                    'not a data-quality issue. Contact AFS support before retrying.'
                ))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('request_xml'):
                vals['request_xml'] = afs_client.redact_secure_key(vals['request_xml'])
        return super().create(vals_list)

    def write(self, vals):
        if set(vals.keys()) - _LINK_ONLY_FIELDS:
            for rec in self:
                if rec.state in TERMINAL_STATES:
                    raise UserError(_('This AFS transaction is final and cannot be edited.'))
        if 'request_xml' in vals and vals['request_xml']:
            vals['request_xml'] = afs_client.redact_secure_key(vals['request_xml'])
        return super().write(vals)

    def unlink(self):
        for rec in self:
            if rec.state != 'error' or not self.env.user.has_group('afs_pos_integration.group_afs_manager'):
                raise UserError(_(
                    'AFS transactions cannot be deleted. This is the audit trail for '
                    'card payments and must remain intact.'
                ))
        return super().unlink()

    def action_void(self):
        """Starts the Void and returns immediately — mirrors
        afs.payment.wizard.action_confirm exactly, and for the same reason:
        a blocking SOAP call inside a request (here, typically inside
        account.move.action_post() for the credit note that triggers this)
        risks a stuck "loading" UI, and worse, has no checkpoint commit, so
        if anything after the actual Void call fails, the WHOLE request rolls
        back — even though AFS may have already genuinely reversed the
        charge. Persisting a 'pending' void record and committing before the
        blocking call closes that gap, exactly as it does for Sale."""
        self.ensure_one()
        if self.operation != 'sale' or self.state != 'approved':
            raise UserError(_('Only an approved sale transaction can be reversed with Void.'))
        if self.reversed_by_id:
            raise UserError(_('This sale has already been reversed.'))
        if self.env['afs.transaction'].search_count([('reverses_id', '=', self.id), ('state', '=', 'pending')]):
            raise UserError(_('A Void is already in progress for this sale.'))

        terminal = self.terminal_id
        if (terminal.void_requires_open_batch and terminal.last_settlement_date
                and self.pos_datetime and terminal.last_settlement_date > self.pos_datetime):
            raise UserError(_(
                'Batch already settled. Refund must be processed through AFS '
                '(terminal or AFS portal), not through Void.'
            ))

        # Advisory lock shared with Sale (afs.payment.wizard): same physical
        # terminal, so a Sale and a Void can never legitimately run at once.
        lock_key = afs_client.TERMINAL_ADVISORY_LOCK_OFFSET + terminal.id
        self.env.cr.execute('SELECT pg_try_advisory_lock(%s)', (lock_key,))
        if not self.env.cr.fetchone()[0]:
            raise UserError(_('Terminal "%s" is currently busy with another transaction.') % terminal.name)

        try:
            void_txn = self._start_void_async(terminal, lock_key)
        except Exception:
            self.env.cr.execute('SELECT pg_advisory_unlock(%s)', (lock_key,))
            raise
        return void_txn

    def _start_void_async(self, terminal, lock_key):
        token = uuid.uuid4().hex[:8].upper()
        printer = afs_client.AfsPrinter(
            invoice_number=token,
            reference_number=token,
            enable_print_pos_receipt=int(terminal.print_pos_receipt),
            enable_print_receipt_note=int(terminal.print_receipt_note),
            printer_width=terminal.printer_width,
        )
        config = terminal._get_config()
        request_xml = afs_client.build_void_envelope(
            config, self.pos_auth_code, self.pos_invoice_number, self.pos_rrn, printer)

        # Persist BEFORE the blocking call and commit now — see action_void's
        # docstring. Do not remove this commit as "unnecessary ORM code".
        void_txn = self.env['afs.transaction'].create({
            'move_id': self.move_id.id,
            'terminal_id': terminal.id,
            'company_id': self.company_id.id,
            'operation': 'void',
            'reverses_id': self.id,
            'requested_amount': self.pos_amount,
            'currency_id': self.currency_id.id,
            'our_reference': token,
            'request_xml': request_xml,
            'state': 'pending',
            'started_at': fields.Datetime.now(),
        })
        self.env.cr.commit()

        args = (self.env.cr.dbname, self.env.uid, dict(self.env.context),
                self.id, void_txn.id, request_xml, lock_key)
        if self.env.registry.in_test_mode():
            # Same reasoning as afs.payment.wizard._start_sale_async: a real
            # OS thread under TransactionCase would share the test's own
            # cursor across two threads — run inline instead.
            self._run_void_background(*args)
        else:
            threading.Thread(target=self._run_void_background, args=args, daemon=True).start()
        return void_txn

    def _run_void_background(self, dbname, uid, context, sale_txn_id, void_txn_id, request_xml, lock_key):
        """Runs off-thread — must not touch self.env (see the equivalent note
        on afs.payment.wizard._run_sale_background)."""
        move_id = None
        registry = odoo.modules.registry.Registry(dbname)
        try:
            with registry.cursor() as cr:
                env = api.Environment(cr, uid, context)
                sale_txn = env['afs.transaction'].browse(sale_txn_id)
                terminal = sale_txn.terminal_id
                move_id = sale_txn.move_id.id
                client = terminal._get_client()
                try:
                    response_xml = client.post('Void', request_xml)
                except Exception as exc:  # noqa: BLE001 - Void has no documented recovery path (unlike Sale/EnquiryByRef)
                    env['afs.transaction']._safe_finalize_by_id(void_txn_id, {
                        'state': 'unknown',
                        'error_message': _(
                            'Void call failed or the connection dropped (%s). Outcome unknown — do '
                            'not retry blindly. Verify on the terminal or AFS portal whether the '
                            'reversal actually went through before doing anything else.'
                        ) % exc,
                    }, late_reason=_('the Void was already cancelled/finalized before this response arrived'))
                    return
                result = afs_client.parse_response(response_xml)
                vals = {
                    'response_xml': response_xml,
                    'state': 'approved' if result.approved else 'error',
                    'error_message': '' if result.approved else (result.pos_resp_text or result.web_response_error),
                    'pos_amount': result.pos_amount or 0.0,
                    'pos_currency_code': result.pos_currency_code,
                    'pos_auth_code': result.pos_auth_code,
                    'pos_rrn': result.pos_rrn,
                    'pos_stan': result.pos_stan,
                    'pos_invoice_number': result.pos_invoice_number,
                    'pos_batch_number': result.pos_batch_number,
                    'pos_resp_code': result.pos_resp_code,
                    'pos_resp_text': result.pos_resp_text,
                    'pos_resp_status': result.pos_resp_status,
                    'pos_pan': result.pos_pan,
                    'pos_issuer_name': result.pos_issuer_name,
                    'pos_scheme': result.pos_scheme,
                    'pos_entry_mode': result.pos_entry_mode,
                    'pos_datetime': result.pos_datetime,
                    'pos_receipt': result.pos_receipt,
                    'web_response_status': result.web_response_status,
                    'web_response_error': result.web_response_error,
                }
                wrote = env['afs.transaction']._safe_finalize_by_id(
                    void_txn_id, vals,
                    late_reason=_('the Void was already cancelled/finalized before this response arrived'),
                )
                if result.approved and wrote:
                    sale_txn.write({'reversed_by_id': void_txn_id})
                    sale_txn._reverse_payment()
                    if move_id:
                        env['account.move'].browse(move_id).message_post(body=_(
                            'Card payment reversed via AFS Void. Auth: %s, RRN: %s'
                        ) % (result.pos_auth_code, result.pos_rrn))
                elif result.approved and not wrote:
                    if move_id:
                        env['account.move'].browse(move_id).message_post(body=_(
                            'AFS approved a Void (auth %s / RRN %s) for a reversal that had already '
                            'been cancelled/finalized on our side. The card WAS refunded — recorded '
                            'on a new linked transaction, but the original sale was NOT marked '
                            'reversed and the payment was NOT unreconciled. Resolve manually.'
                        ) % (result.pos_auth_code, result.pos_rrn))
                elif wrote:
                    if move_id:
                        env['account.move'].browse(move_id).message_post(body=_(
                            'Void was not approved by the terminal: %s'
                        ) % (result.pos_resp_text or result.web_response_error or _('unknown error')))
                elif move_id:
                    env['account.move'].browse(move_id).message_post(body=_(
                        'A late Void response (not approved) arrived after this reversal attempt '
                        'was already cancelled/finalized: %s'
                    ) % (result.pos_resp_text or result.web_response_error or _('unknown error')))
                cr.commit()
        except Exception:  # noqa: BLE001 - never let a background thread crash silently
            _logger.exception('AFS background Void thread failed for afs.transaction %s', void_txn_id)
        finally:
            try:
                with registry.cursor() as cr:
                    cr.execute('SELECT pg_advisory_unlock(%s)', (lock_key,))
                    if move_id:
                        env2 = api.Environment(cr, uid, context)
                        env2['bus.bus']._sendone(
                            env2.user.partner_id, 'afs_pos_integration/payment_update', {'move_id': move_id})
                    cr.commit()
            except Exception:  # noqa: BLE001
                _logger.exception('Failed to release AFS advisory lock %s / notify for void %s', lock_key, void_txn_id)

    @api.model
    def _safe_finalize_by_id(self, txn_id, vals, *, late_reason):
        """Race-safe write of a terminal result onto a 'pending' afs.transaction.

        Shared by both the Sale wizard and Void above. Returns True if it
        wrote to the original record. If the record was already finalized by
        something else first (most commonly: the user clicked Cancel Payment
        while this response was in flight), the result is NEVER silently
        dropped — it's recorded as a new transaction linked to the original
        via reverses_id, and this returns False so the caller can react (e.g.
        flag a late approval loudly, since that means real money moved
        despite the cancellation).

        Race-safety: `SELECT ... FOR UPDATE` takes the same row-level lock a
        plain UPDATE would (e.g. from action_request_cancellation's own
        self.write()), so the two can never both "win" — whichever commits
        first determines what the other sees.
        """
        with self.env.registry.cursor() as new_cr:
            new_env = api.Environment(new_cr, self.env.uid, self.env.context)
            new_cr.execute("SELECT id FROM afs_transaction WHERE id = %s AND state = 'pending' FOR UPDATE", (txn_id,))
            if new_cr.fetchone():
                new_env['afs.transaction'].browse(txn_id).write(vals)
                new_cr.commit()
                return True

        with self.env.registry.cursor() as new_cr:
            new_env = api.Environment(new_cr, self.env.uid, self.env.context)
            original = new_env['afs.transaction'].browse(txn_id)
            late_vals = dict(vals)
            late_vals.update({
                'move_id': original.move_id.id,
                'terminal_id': original.terminal_id.id,
                'company_id': original.company_id.id,
                'operation': original.operation,
                'reverses_id': txn_id,
                'requested_amount': original.requested_amount,
                'currency_id': original.currency_id.id,
                'our_reference': original.our_reference,
                'request_xml': original.request_xml,
            })
            late_vals['error_message'] = _(
                'This response arrived after %s (original afs.transaction #%s). Recorded '
                'separately so the outcome is never silently lost.'
            ) % (late_reason, txn_id)
            new_env['afs.transaction'].create(late_vals)
            new_cr.commit()
        return False

    def action_request_cancellation(self):
        """For when the client walks away and the terminal is still
        prompting: tells the terminal to stop waiting. Works on a pending
        Sale or a pending Void alike — only meaningful while this record is
        still 'pending'. Per AFS, this only stops a request before it
        reaches the terminal; once it's already there prompting, this still
        cleans up our own side (frees the record so the real, possibly late,
        outcome is handled by _safe_finalize_by_id instead of silently lost),
        but may not stop the physical device."""
        self.ensure_one()
        if self.state != 'pending':
            raise UserError(_('Request Cancellation only applies to a pending transaction.'))
        terminal = self.terminal_id
        config = terminal._get_config()
        client = terminal._get_client()
        try:
            request_xml, response_xml, result = client.cancel(config)
        except Exception as exc:  # noqa: BLE001
            raise UserError(_('Request Cancellation failed: %s') % exc)

        self.env['afs.transaction'].create({
            'move_id': self.move_id.id,
            'terminal_id': terminal.id,
            'company_id': self.company_id.id,
            'operation': 'cancel',
            'reverses_id': self.id,
            'request_xml': request_xml,
            'response_xml': response_xml,
            'state': 'approved' if result.approved else 'unknown',
            'web_response_status': result.web_response_status,
            'pos_resp_code': result.pos_resp_code,
            'pos_resp_text': result.pos_resp_text,
        })
        self.write({'state': 'error', 'error_message': _('Cancelled by user request.')})

    def _reverse_payment(self):
        """Unreconcile and cancel the payment linked to this sale.
        NOTE: confirmed with Accounts as the default presentation; if they
        prefer a credit note posted against the clearing journal instead,
        change this method rather than bypassing it."""
        self.ensure_one()
        payment = self.payment_id
        if not payment or payment.state == 'cancel':
            return
        move = payment.move_id
        reconciled_lines = move.line_ids.filtered(
            lambda l: l.account_id.reconcile and (l.matched_debit_ids or l.matched_credit_ids)
        )
        if reconciled_lines:
            reconciled_lines.remove_move_reconcile()
        if payment.state == 'posted':
            payment.action_cancel()

    def _cron_sweep_stale_pending(self):
        """Safety net for the async Sale flow: a background thread can die
        with its worker (redeploy, crash) without ever finalising its
        transaction. Anything still 'pending' well past its own terminal's
        timeout is not going to resolve on its own — flag it the same way a
        dropped connection with no recoverable outcome would be flagged."""
        now = fields.Datetime.now()
        for txn in self.search([('state', '=', 'pending'), ('started_at', '!=', False)]):
            grace = (txn.terminal_id.timeout or 180) + 120
            if txn.started_at <= now - timedelta(seconds=grace):
                txn.write({
                    'state': 'unknown',
                    'error_message': _(
                        'No response was received within the expected window; the '
                        'background worker may have been interrupted. Outcome unknown '
                        '— do not retry. Verify on the terminal or AFS portal.'
                    ),
                })
