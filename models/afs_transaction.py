# -*- coding: utf-8 -*-
import functools
import logging
import re
import threading
import uuid
from datetime import timedelta

import psycopg2.errors
import requests

import odoo
from odoo import SUPERUSER_ID, api, fields, models, _
from odoo.exceptions import UserError, ValidationError

from ..services import afs_client
from .afs_terminal import check_afs_manager, check_afs_user

_logger = logging.getLogger(__name__)

TERMINAL_STATES = ('approved', 'declined', 'cancelled', 'unknown', 'error')
# Plumbing the workflow must be able to set even after a record has reached a
# terminal state — linkage and bookkeeping, not rewriting history. Users have
# read-only access to this model, so only the module's own (sudo) code can
# ever reach write() at all.
_LINK_ONLY_FIELDS = {'reversed_by_id', 'payment_id', 'finished_at'}
NOTIFICATION_TYPE = 'afs_pos_integration/payment_update'
# Terminal's answer to a Void of a sale that is already voided (seen live:
# PosRespCode "uc", PosRespText "Transaction has already been voided").
ALREADY_VOIDED_RE = re.compile(r'already\s+(?:been\s+)?void', re.IGNORECASE)


class AfsTransaction(models.Model):
    _name = 'afs.transaction'
    _description = 'AFS Terminal Transaction'
    _order = 'create_date desc'

    # Link
    move_id = fields.Many2one('account.move', string='Invoice', index=True, ondelete='restrict')
    credit_note_id = fields.Many2one(
        'account.move', string='Credit Note', ondelete='restrict',
        help='For a Void triggered by posting a credit note: that credit note. It is '
             'reconciled against the invoice once the Void is approved.',
    )
    terminal_id = fields.Many2one('afs.terminal', string='Terminal', required=True, ondelete='restrict')
    payment_id = fields.Many2one('account.payment', string='Payment', ondelete='set null')
    reverses_id = fields.Many2one('afs.transaction', string='Reverses', ondelete='restrict')
    reversed_by_id = fields.Many2one('afs.transaction', string='Reversed By', ondelete='set null')
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)

    # Request
    operation = fields.Selection([
        ('sale', 'Sale'),
        ('void', 'Void'),
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
        ('cancelled', 'Cancelled'),
        ('unknown', 'Unknown'),
        ('error', 'Error'),
    ], required=True, default='pending', index=True)
    cancel_requested_at = fields.Datetime(
        help='When a user asked AFS to cancel this pending Sale/Void. The record stays '
             'pending until the terminal answers: "Cancelled" means the card was not '
             'charged; an approval that still comes back is recorded as approved.')
    cancel_response_status = fields.Char(help="AFS's answer to the RequestCancellation call.")
    cancel_response_xml = fields.Text()
    reversal_incomplete = fields.Boolean(
        compute='_compute_reversal_incomplete', compute_sudo=True,
        help='An approved Void whose sale is not yet marked reversed, or whose card '
             'payment is not yet cancelled.')
    error_message = fields.Text()
    started_at = fields.Datetime(
        help='When the background Sale/Void call actually started — used to tell '
             'whether the terminal is still busy, and by the stale-pending cron.'
    )
    finished_at = fields.Datetime(
        help='When the background Sale/Void call returned (whatever the outcome). '
             'Until then the terminal counts as busy.'
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
        # Backstop only: every terminal response goes through
        # afs.terminal._parse_response, which masks the PAN first.
        for rec in self:
            if rec.pos_pan and not afs_client.is_masked_pan(rec.pos_pan):
                raise ValidationError(_(
                    'The card number does not look like a masked PAN. Refusing to '
                    'store it — this would be a PCI incident, not a data-quality issue.'
                ))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('request_xml'):
                vals['request_xml'] = afs_client.redact_secure_key(vals['request_xml'])
        return super().create(vals_list)

    def write(self, vals):
        if set(vals.keys()) - _LINK_ONLY_FIELDS:
            manual = self.env.context.get('afs_manual_resolution')
            for rec in self:
                # The one exception: a manager resolving an Unknown outcome
                # through afs.resolve.wizard (users cannot write here at all).
                if rec.state in TERMINAL_STATES and not (manual and rec.state == 'unknown'):
                    raise UserError(_('This AFS transaction is final and cannot be edited.'))
        if 'request_xml' in vals and vals['request_xml']:
            vals['request_xml'] = afs_client.redact_secure_key(vals['request_xml'])
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_never(self):
        raise UserError(_(
            'AFS transactions cannot be deleted. This is the audit trail for '
            'card payments and must remain intact.'
        ))

    @staticmethod
    def _result_to_vals(result):
        """Stored fields for a parsed AFS response (pass it through
        afs.terminal._parse_response first: PAN masking, terminal time -> UTC)."""
        return {
            'response_xml': result.response_xml,
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

    # ------------------------------------------------------------------
    # Starting a terminal call
    # ------------------------------------------------------------------

    @api.model
    def _create_pending_locked(self, terminal, vals, move=None, sale_to_void=None):
        """Create the 'pending' record on its OWN cursor and commit it before
        any blocking call — if the background thread dies (worker restart,
        crash), an uncommitted record would vanish, leaving a charged client
        with no trace. Using a separate cursor instead of committing the
        request's cursor keeps the caller's transaction (e.g. a credit note
        being posted) atomic.

        All the "is this allowed right now" checks run on that same cursor,
        under row locks, so two concurrent starts cannot both pass them.
        Returns the new record's id."""
        self.env.flush_all()
        with self.env.registry.cursor() as cr:
            env = self.env(cr=cr, su=True)
            if move:
                try:
                    cr.execute('SELECT id FROM account_move WHERE id = %s FOR NO KEY UPDATE NOWAIT', (move.id,))
                except psycopg2.errors.LockNotAvailable:
                    raise UserError(_('A card payment is already in progress for this invoice.'))
                if env['afs.transaction'].search_count([('move_id', '=', move.id), ('state', '=', 'pending')]):
                    raise UserError(_('A card payment is already pending for this invoice.'))
                if vals.get('operation') == 'sale':
                    fresh_move = env['account.move'].browse(move.id)
                    if fresh_move.currency_id.compare_amounts(vals['requested_amount'], fresh_move.amount_residual) > 0:
                        raise UserError(_('The amount to charge cannot exceed the invoice residual (%s).')
                                        % fresh_move.amount_residual)
            if sale_to_void:
                try:
                    cr.execute('SELECT id FROM afs_transaction WHERE id = %s FOR NO KEY UPDATE NOWAIT', (sale_to_void.id,))
                except psycopg2.errors.LockNotAvailable:
                    raise UserError(_('A Void is already in progress for this sale.'))
                fresh_sale = env['afs.transaction'].browse(sale_to_void.id)
                if fresh_sale.reversed_by_id:
                    raise UserError(_('This sale has already been reversed.'))
                if env['afs.transaction'].search_count([('reverses_id', '=', fresh_sale.id), ('operation', '=', 'void'), ('state', '=', 'pending')]):
                    raise UserError(_('A Void is already in progress for this sale.'))
            env['afs.terminal'].browse(terminal.id)._lock_and_check_idle()
            txn = env['afs.transaction'].create(vals)
            txn_id = txn.id
        return txn_id

    def _launch(self, method_name, txn_id):
        args = (self.env.cr.dbname, self.env.uid, dict(self.env.context), txn_id)
        target = getattr(self.env['afs.transaction'], method_name)
        if self.env.registry.in_test_mode():
            # No real OS thread under TransactionCase: Registry(dbname).cursor()
            # hands back a TestCursor wrapping the SAME underlying connection
            # as the test's own cursor, and using that from two concurrent
            # threads is a real corruption risk. Run inline instead — same
            # code path, same outcome, just synchronous.
            target(*args)
        else:
            threading.Thread(target=target, args=args, daemon=True).start()

    @staticmethod
    def _build_printer(terminal, token):
        return afs_client.AfsPrinter(
            invoice_number=token,
            reference_number=token,
            enable_print_pos_receipt=int(terminal.print_pos_receipt),
            enable_print_receipt_note=int(terminal.print_receipt_note),
            printer_width=terminal.printer_width,
        )

    @staticmethod
    def _finish_and_notify(registry, uid, context, txn_id, move_id):
        """Always runs when a background call ends, whatever happened: frees
        the terminal (finished_at) and tells the browser to refresh."""
        try:
            with registry.cursor() as cr:
                # Plain SQL: bookkeeping only, and must not trip over the
                # final-state write guard or any ORM side effect.
                cr.execute("UPDATE afs_transaction SET finished_at = (now() AT TIME ZONE 'UTC') WHERE id = %s", (txn_id,))
                if move_id:
                    env = api.Environment(cr, uid, context)
                    env['afs.transaction']._notify_move_update(move_id)
        except Exception:  # noqa: BLE001
            _logger.exception('Failed to mark AFS transaction %s finished / notify', txn_id)

    @api.model
    def _notify_move_update(self, move_id):
        # Every AFS user, not just whoever started it: anyone looking at the
        # invoice should see it update. The payload is only the invoice id.
        group = self.env.ref('afs_pos_integration.group_afs_user', raise_if_not_found=False)
        if not group:
            return
        for partner in group.sudo().users.partner_id:
            self.env['bus.bus']._sendone(partner, NOTIFICATION_TYPE, {'move_id': move_id})

    @api.model
    def _post_chatter(self, move_id, body):
        if not move_id:
            return
        with self.env.registry.cursor() as new_cr:
            new_env = api.Environment(new_cr, self.env.uid, self.env.context)
            new_env['account.move'].browse(move_id).sudo().message_post(body=body, author_id=new_env.user.partner_id.id)

    # ------------------------------------------------------------------
    # Sale
    # ------------------------------------------------------------------

    @api.model
    def _start_sale(self, move, terminal, amount):
        """Starts the Sale and returns immediately; the terminal wait happens
        in a background thread and its outcome is pushed over the bus."""
        terminal._get_config()  # fail now, before anything is recorded, if the secret is missing
        terminal._get_client()
        txn_id = self._create_pending_locked(terminal, {
            'move_id': move.id,
            'terminal_id': terminal.id,
            'company_id': move.company_id.id,
            'operation': 'sale',
            'requested_amount': amount,
            'currency_id': move.currency_id.id,
            'our_reference': _new_reference(),
            'started_at': fields.Datetime.now(),
        }, move=move)
        self._launch('_run_sale_background', txn_id)
        return txn_id

    @api.model
    def _run_sale_background(self, dbname, uid, context, txn_id):
        """Runs off-thread: must not touch self.env — that cursor belongs to
        the request that already returned.

        No database cursor is held while waiting on the terminal (up to the
        terminal timeout): one short cursor prepares the call, and a NEW one —
        with a fresh snapshot — records the outcome. Odoo runs at REPEATABLE
        READ: a cursor opened before the wait cannot write rows that other
        cursors updated during it (e.g. the invoice's card status) and would
        roll back everything with "could not serialize access"."""
        move_id = None
        registry = odoo.modules.registry.Registry(dbname)
        try:
            with registry.cursor() as cr:
                env = api.Environment(cr, uid, context)
                txn = env['afs.transaction'].sudo().browse(txn_id)
                terminal = txn.terminal_id
                move_id = txn.move_id.id
                config = terminal._get_config()
                client = terminal._get_client()
                request_xml = afs_client.build_sale_envelope(
                    config, txn.requested_amount, self._build_printer(terminal, txn.our_reference))
                if not txn._safe_finalize_by_id(txn_id, {'request_xml': request_xml}, late_reason=None):
                    # Cancelled before we even reached the terminal: never charge.
                    return

            response_xml = transport_exc = None
            try:
                response_xml = client.post('Sale', request_xml)
            except Exception as exc:  # noqa: BLE001 - classified below
                transport_exc = exc

            with registry.cursor() as cr:
                env = api.Environment(cr, uid, context)
                txn = env['afs.transaction'].sudo().browse(txn_id)
                terminal = txn.terminal_id
                if _outcome_unknown(transport_exc):
                    txn._handle_dropped_connection(terminal, config, transport_exc)
                elif transport_exc is not None:
                    txn._safe_finalize_by_id(txn_id, {'state': 'error', 'error_message': str(transport_exc)},
                                             late_reason=_('an error was being recorded after the transaction was already finalized'))
                else:
                    txn._finalize_sale(terminal._parse_response(response_xml))
        except Exception:  # noqa: BLE001 - never let a background thread crash silently
            _logger.exception('AFS background Sale thread failed for afs.transaction %s', txn_id)
        finally:
            self._finish_and_notify(registry, uid, context, txn_id, move_id)

    def _finalize_sale(self, result):
        self.ensure_one()
        state = afs_client.classify(result)
        vals = self._approved_sale_vals(result) if result.approved else dict(self._result_to_vals(result), state=state)
        wrote = self._safe_finalize_by_id(
            self.id, vals,
            late_reason=_('the original Sale attempt was already cancelled/finalized before this response arrived'),
        )
        move_id = self.move_id.id
        if wrote:
            if state == 'approved' and self._fresh_cancel_requested():
                self._post_chatter(move_id, _(
                    'Card was charged (auth %s / RRN %s) even though Cancel was pressed — the '
                    'terminal had already completed the payment. The payment has been registered.'
                ) % (result.pos_auth_code, result.pos_rrn))
            elif state == 'cancelled':
                self._post_chatter(move_id, _('Card payment cancelled on the terminal — the card was not charged.'))
            elif state == 'declined':
                self._post_chatter(move_id, _('Card declined: %s (%s)') % (result.pos_resp_text, result.pos_resp_code))
            elif state == 'unknown':
                self._post_chatter(move_id, _(
                    'AFS response could not be classified as approved or declined. '
                    'Outcome unknown — do not retry. Check the terminal / AFS portal.'
                ))
        elif state == 'approved':
            self._warn_late_charge(result)

    def _approved_sale_vals(self, result):
        """Outcome vals for an approved Sale, with the payment already
        registered — regardless of what happens next to our own bookkeeping
        row. If the card was charged, that must never depend on winning a race
        against a Cancel click."""
        self.ensure_one()
        vals = dict(self._result_to_vals(result), state='approved')
        payment_id, payment_error = self._register_payment(result)
        if payment_id:
            vals['payment_id'] = payment_id
        if payment_error:
            vals['error_message'] = payment_error
            self._post_chatter(self.move_id.id, _(
                'Card charged successfully but payment registration failed: %s') % payment_error)
        return vals

    def _warn_late_charge(self, result):
        """An approval that lost the race against a Cancel / an earlier
        finalisation: real money moved, so say so loudly."""
        self._post_chatter(self.move_id.id, _(
            'Card was charged (auth %s / RRN %s) AFTER this Sale attempt had already been '
            'cancelled/finalized — the cancellation did not stop the terminal in time. Payment '
            'has been registered on a new, separate transaction. Please verify on the terminal/AFS '
            'portal that the card was not charged twice.'
        ) % (result.pos_auth_code, result.pos_rrn))

    def _handle_dropped_connection(self, terminal, config, exc):
        """Recovers a lost Sale answer by asking AFS for this exact attempt by
        our own reference (EnquiryByRef) — not a guess at "whatever the
        terminal's last transaction was"."""
        self.ensure_one()
        _logger.warning('AFS Sale connection dropped for afs.transaction %s: %s', self.id, exc)
        token = self.our_reference
        move_id = self.move_id.id
        late_reason = _('a recovery attempt (EnquiryByRef) resolved after the original attempt was already finalized')
        try:
            result = terminal._parse_response(_post_enquiry_by_ref(terminal._get_client(), config, token))
        except Exception as enquiry_exc:  # noqa: BLE001
            self._safe_finalize_by_id(self.id, {
                'state': 'unknown',
                'error_message': _(
                    'Connection dropped (%s) and recovery EnquiryByRef also failed: %s'
                ) % (exc, enquiry_exc),
            }, late_reason=late_reason)
            return

        if (_afs_has_record(result) and result.approved
                and afs_client.correlate_recovery(result, self.requested_amount, self.started_at)):
            if not self._safe_finalize_by_id(self.id, self._approved_sale_vals(result), late_reason=late_reason):
                self._warn_late_charge(result)
            return

        outcome = afs_client.classify(result)
        if outcome in ('declined', 'cancelled'):
            # Keyed lookup, not a guess: trust a declined/cancelled outcome for our own reference.
            vals = self._result_to_vals(result)
            vals['state'] = outcome
            if self._safe_finalize_by_id(self.id, vals, late_reason=late_reason):
                self._post_chatter(move_id, _(
                    'Card not charged — %s, confirmed by AFS after a dropped connection: %s %s'
                ) % (outcome, result.pos_resp_text or result.web_response_error or '', result.pos_resp_code or ''))
            return

        self._safe_finalize_by_id(self.id, {
            'state': 'unknown',
            'error_message': _(
                'Connection dropped during Sale (%s). Recovery EnquiryByRef for reference %s '
                'did not return a confirmed outcome. Outcome unknown — do not retry. Verify on '
                'the terminal or AFS portal before taking any action.'
            ) % (exc, token),
            'response_xml': result.response_xml,
        }, late_reason=late_reason)

    def _register_payment(self, result):
        """Returns (payment_id, error_message). Never finalises the transaction
        itself — the caller merges the outcome into its single final write, so
        a posting failure cannot produce a second, duplicate 'approved' sale."""
        self.ensure_one()
        with self.env.registry.cursor() as new_cr:
            # The user's own rights, not sudo: posting a payment is an
            # accounting action and must be attributable and permitted.
            new_env = api.Environment(new_cr, self.env.uid, self.env.context)
            move = new_env['account.move'].browse(self.move_id.id)
            terminal = new_env['afs.terminal'].sudo().browse(self.terminal_id.id)
            requested = self.requested_amount
            amount = float(result.pos_amount) if result.pos_amount is not None else requested
            if result.pos_amount is not None and move.currency_id.compare_amounts(amount, requested) != 0:
                _logger.warning(
                    'AFS captured amount (%s) differs from requested amount (%s) for move %s; '
                    'using the captured amount.', amount, requested, move.id)
            try:
                payment = new_env['afs.transaction']._create_card_payment(
                    move, terminal.journal_id, amount,
                    'AFS %s/%s (term inv %s)' % (result.pos_auth_code, result.pos_rrn, result.pos_invoice_number))
            except Exception as exc:  # noqa: BLE001
                new_cr.rollback()
                # Do NOT auto-void: the card has already been charged. A human
                # must reconcile this manually; an automatic reversal triggered
                # by a transient DB/posting error would be worse.
                _logger.exception('AFS: card charged but payment registration failed for move %s', move.id)
                return False, _(
                    'Card was charged (auth %s / RRN %s) but the Odoo payment could '
                    'not be posted: %s. Resolve manually — do not re-charge.'
                ) % (result.pos_auth_code, result.pos_rrn, exc)
            return payment.id, None

    @api.model
    def _create_card_payment(self, move, journal, amount, communication):
        """Registers a card payment on `move` with this environment's rights
        (callers pass the user's own, not sudo: posting a payment is an
        accounting action). Raises on failure. Returns the payment."""
        inbound_line = journal.inbound_payment_method_line_ids[:1]
        ctx = {'active_model': 'account.move', 'active_ids': move.ids, 'active_id': move.id}
        payments = self.env['account.payment.register'].with_context(**ctx).create({
            'amount': amount,
            'journal_id': journal.id,
            'payment_method_line_id': inbound_line.id,
            'payment_date': fields.Date.context_today(self),
            'communication': communication,
        })._create_payments()
        return payments[:1]

    # ------------------------------------------------------------------
    # Void
    # ------------------------------------------------------------------

    def _check_voidable(self):
        """Everything that can be checked before touching the terminal. Raises
        UserError — so when called while posting a credit note, the posting
        itself is refused rather than silently skipping the Void."""
        self.ensure_one()
        if self.operation != 'sale' or self.state != 'approved':
            raise UserError(_('Only an approved sale transaction can be reversed with Void.'))
        if self.reversed_by_id:
            raise UserError(_('This sale has already been reversed.'))
        if self.search_count([('reverses_id', '=', self.id), ('operation', '=', 'void'), ('state', '=', 'pending')]):
            raise UserError(_('A Void is already in progress for this sale.'))
        terminal = self.terminal_id
        self._check_void_delay()
        if (terminal.void_requires_open_batch and terminal.last_settlement_date
                and self.pos_datetime and terminal.last_settlement_date > self.pos_datetime):
            raise UserError(_(
                'Batch already settled. Refund must be processed through AFS '
                '(terminal or AFS portal), not through Void.'
            ))
        terminal._get_config()
        terminal._get_client()

    def _check_void_delay(self):
        """Refuse a Void that would follow its sale too closely: AFS loses
        the answer to such a Void (see afs.terminal.void_delay_seconds)."""
        self.ensure_one()
        delay = self.terminal_id.void_delay_seconds
        done_at = self.finished_at or self.pos_datetime
        if delay <= 0 or not done_at:
            return
        elapsed = (fields.Datetime.now() - done_at).total_seconds()
        if elapsed < delay:
            raise UserError(_(
                'This card payment was completed only %(elapsed)s seconds ago. AFS needs '
                'time to finish processing the sale before it can return a Void result. '
                'Please wait %(remaining)s more seconds and try again.',
                elapsed=max(int(elapsed), 0), remaining=int(delay - elapsed) + 1,
            ))

    def action_void(self):
        """Direct Void from the transaction form (no credit note). Starts the
        Void and returns immediately — same reasoning as the Sale: a blocking
        SOAP call inside a request risks a stuck UI and, worse, a rollback
        after AFS has already genuinely reversed the charge."""
        self.ensure_one()
        check_afs_manager(self.env)
        sale = self.sudo()
        sale._check_voidable()
        return sale._start_void()

    def _start_void(self, credit_note=None):
        self.ensure_one()
        terminal = self.terminal_id
        void_id = self._create_pending_locked(terminal, {
            'move_id': self.move_id.id,
            'credit_note_id': credit_note.id if credit_note else False,
            'terminal_id': terminal.id,
            'company_id': self.company_id.id,
            'operation': 'void',
            'reverses_id': self.id,
            'requested_amount': self.pos_amount,
            'currency_id': self.currency_id.id,
            'our_reference': _new_reference(),
            'started_at': fields.Datetime.now(),
        }, sale_to_void=self)
        self._launch('_run_void_background', void_id)
        return self.browse(void_id)

    @api.model
    def _run_void_background(self, dbname, uid, context, void_txn_id):
        """Runs off-thread — must not touch self.env. Same cursor discipline
        as _run_sale_background: nothing held during the terminal wait, a
        fresh cursor for every write afterwards."""
        move_id = None
        registry = odoo.modules.registry.Registry(dbname)
        late_reason = _('the Void was already cancelled/finalized before this response arrived')
        try:
            with registry.cursor() as cr:
                env = api.Environment(cr, uid, context)
                void_txn = env['afs.transaction'].sudo().browse(void_txn_id)
                sale_txn = void_txn.reverses_id
                terminal = void_txn.terminal_id
                move_id = void_txn.move_id.id
                config = terminal._get_config()
                client = terminal._get_client()
                # Original terminal invoice number only, auth code and RRN left
                # empty — spec §3.2 ("one of the following fields") and the
                # DLL's VoidByInvoice(invoiceNumber), as in the AFS Postman
                # sample. Lost Void answers (HTTP 524) were caused by sending the
                # Void seconds after its Sale, not by these fields: see
                # afs.terminal.void_delay_seconds.
                request_xml = afs_client.build_void_envelope(
                    config, '', sale_txn.pos_invoice_number, '',
                    self._build_printer(terminal, void_txn.our_reference))
                void_ref = void_txn.our_reference
                if not void_txn._safe_finalize_by_id(void_txn_id, {'request_xml': request_xml}, late_reason=None):
                    return

            response_xml = transport_exc = None
            try:
                response_xml = client.post('Void', request_xml)
            except Exception as exc:  # noqa: BLE001 - recovered by reference below
                transport_exc = exc
                # Same recovery as a lost Sale: ask AFS for THIS Void by the
                # reference we sent with it (verified live: EnquiryByRef finds
                # a transaction by our reference, and returns nothing for one
                # AFS never processed).
                recovered = self._recover_by_reference(client, config, void_ref)
                if recovered is not None:
                    response_xml = recovered

            with registry.cursor() as cr:
                env = api.Environment(cr, uid, context)
                void_txn = env['afs.transaction'].sudo().browse(void_txn_id)
                if response_xml is None:
                    void_txn._safe_finalize_by_id(void_txn_id, {
                        'state': 'unknown',
                        'error_message': _(
                            'Void call failed or the connection dropped (%s), and AFS has no record of '
                            'this Void (EnquiryByRef %s). Outcome unknown — do not retry blindly. Check '
                            'the terminal receipt or AFS portal: if the refund really went through, a '
                            'manager can confirm it on this transaction.'
                        ) % (transport_exc, void_ref),
                    }, late_reason=late_reason)
                    return
                result = void_txn.terminal_id._parse_response(response_xml)
                outcome = afs_client.classify(result)
                if transport_exc is not None:
                    _logger.info('AFS: Void %s recovered via EnquiryByRef after: %s', void_txn_id, transport_exc)
                vals = self._result_to_vals(result)
                vals.update({
                    # A declined Void is an error on our side (nothing reversed);
                    # PosRespStatus -1 stays unknown.
                    'state': 'error' if outcome == 'declined' else outcome,
                    'error_message': '' if result.approved else (result.pos_resp_text or result.web_response_error),
                })
                wrote = void_txn._safe_finalize_by_id(void_txn_id, vals, late_reason=late_reason)

            if result.approved and wrote:
                if not self._run_with_retry(dbname, uid, context, void_txn_id, '_complete_reversal'):
                    self._post_chatter(move_id, _(
                        'The card refund (Void) WAS approved, but the sale could not be marked reversed. '
                        'Open the Void transaction and press "Complete Reversal".'
                    ))
            elif ALREADY_VOIDED_RE.search(result.pos_resp_text or result.web_response_error or ''):
                # Not a failure for the books: the terminal itself confirms the
                # sale is voided — typically by an earlier Void whose answer
                # was lost (Cloudflare 524). Settle that earlier Void.
                self._run_with_retry(dbname, uid, context, void_txn_id, '_resolve_previous_void_from_refusal')
            elif result.approved:
                self._post_chatter(move_id, _(
                    'AFS approved a Void (auth %s / RRN %s) for a reversal that had already '
                    'been cancelled/finalized on our side. The card WAS refunded — recorded '
                    'on a new linked transaction, but the original sale was NOT marked '
                    'reversed and the payment was NOT cancelled. Resolve manually.'
                ) % (result.pos_auth_code, result.pos_rrn))
            elif result.cancelled:
                self._post_chatter(move_id, _('Card refund (Void) cancelled on the terminal — the card was not refunded.'))
            elif wrote:
                self._post_chatter(move_id, _(
                    'Void was not approved by the terminal: %s'
                ) % (result.pos_resp_text or result.web_response_error or _('unknown error')))
            else:
                self._post_chatter(move_id, _(
                    'A late Void response (not approved) arrived after this reversal attempt '
                    'was already cancelled/finalized: %s'
                ) % (result.pos_resp_text or result.web_response_error or _('unknown error')))
        except Exception:  # noqa: BLE001 - never let a background thread crash silently
            _logger.exception('AFS background Void thread failed for afs.transaction %s', void_txn_id)
        finally:
            self._finish_and_notify(registry, uid, context, void_txn_id, move_id)

    @staticmethod
    def _recover_by_reference(client, config, reference):
        """Lost Void answer: the raw EnquiryByRef response XML if AFS has a
        record of this Void, else None (not found, or the enquiry itself
        failed). PosTxnName is deliberately not checked: the spec's own Void
        sample answers PosTxnName "Sale"."""
        try:
            raw = _post_enquiry_by_ref(client, config, reference)
            found = _afs_has_record(afs_client.parse_response(raw))
        except Exception:  # noqa: BLE001
            _logger.exception('AFS: EnquiryByRef %s failed', reference)
            return None
        return raw if found else None

    @api.model
    def _run_with_retry(self, dbname, uid, context, txn_id, method_name):
        """Runs afs.transaction(txn_id).<method_name>() on its own fresh
        cursor, retried on a serialization conflict. Never raises — the
        outcome it follows up on is already recorded. Returns True on success."""
        registry = odoo.modules.registry.Registry(dbname)
        for _attempt in range(3):
            try:
                with registry.cursor() as cr:
                    env = api.Environment(cr, uid, context)
                    getattr(env['afs.transaction'].sudo().browse(txn_id), method_name)()
                return True
            except psycopg2.errors.SerializationFailure:
                _logger.info('AFS: serialization conflict in %s for transaction %s, retrying', method_name, txn_id)
            except Exception:  # noqa: BLE001
                _logger.exception('AFS: %s failed for transaction %s', method_name, txn_id)
                break
        return False

    def _resolve_previous_void_from_refusal(self):
        """self: a Void the terminal refused as "already voided". That answer
        is the terminal's own confirmation that the sale IS voided: if an
        earlier Void of this sale is Unknown (its answer was lost), it is
        that one — mark it approved and complete the reversal."""
        self.ensure_one()
        sale = self.reverses_id
        if not sale or sale.reversed_by_id:
            return
        previous = self.search([
            ('reverses_id', '=', sale.id), ('operation', '=', 'void'),
            ('state', '=', 'unknown'), ('id', '!=', self.id),
        ], order='id desc', limit=1)
        if not previous:
            sale.move_id.sudo().message_post(body=_(
                'The terminal reports this card payment as already voided (Void #%s refused: "%s"), '
                'but Odoo has no record of that Void — it may have been done on the terminal directly. '
                'Check the AFS portal, then cancel the card payment and reconcile manually.'
            ) % (self.id, self.pos_resp_text))
            return
        note = _(
            'Confirmed by the terminal: a later Void (#%(later)s) was refused with "%(text)s", '
            'so this Void went through; its answer had been lost in transit.',
            later=self.id, text=self.pos_resp_text)
        previous._mark_approved_after_unknown({
            'credit_note_id': previous.credit_note_id.id or self.credit_note_id.id,
            'error_message': '\n'.join(filter(None, [previous.error_message, note])),
        })

    def _mark_approved_after_unknown(self, vals):
        """An Unknown Sale/Void found to have gone through after all (manager
        resolution, or the terminal's own "already voided"): record it as
        approved, plus `vals`. For a Void, also do the reversal bookkeeping;
        a Sale's payment is the caller's business."""
        self.ensure_one()
        self.with_context(afs_manual_resolution=True).write(
            dict(vals, state='approved', pos_amount=self.requested_amount))
        if self.operation == 'void':
            self._complete_reversal()

    def _complete_reversal(self):
        """Bookkeeping after an approved Void, in the current transaction:
        mark the sale reversed, then — in a savepoint, with the user's own
        accounting rights — cancel the card payment and reconcile the credit
        note (if the Void came from one) against the invoice, so the invoice
        ends up Reversed and neither document is left open. If the accounting
        part fails (lock date, missing rights), the sale stays marked reversed
        and a message says exactly what to finish by hand.
        Idempotent: safe to run again (the "Complete Reversal" button).
        NOTE: unreconcile + cancel was confirmed with Accounts as the default
        presentation; change this method rather than bypassing it."""
        self.ensure_one()
        sale = self.reverses_id
        if self.operation != 'void' or self.state != 'approved' or not sale or sale.operation != 'sale':
            raise UserError(_('Only an approved Void of a sale can be completed.'))
        invoice = sale.move_id
        if not sale.reversed_by_id:
            sale.write({'reversed_by_id': self.id})
        try:
            with self.env.cr.savepoint():
                self._reverse_payment_and_reconcile()
        except psycopg2.errors.SerializationFailure:
            raise
        except Exception as exc:  # noqa: BLE001
            _logger.exception('AFS: Void %s approved but payment reversal failed', self.id)
            invoice.sudo().message_post(body=_(
                'The card refund (Void) WAS approved, but the payment could not be cancelled / the '
                'credit note could not be reconciled automatically: %s. Cancel the card payment and '
                'reconcile the credit note with this invoice manually, or press "Complete Reversal" '
                'on the Void transaction once the cause is fixed.'
            ) % exc)
            return False
        if self.pos_auth_code or self.pos_rrn:
            body = _('Card payment reversed via AFS Void. Auth: %s, RRN: %s') % (
                self.pos_auth_code or '-', self.pos_rrn or '-')
        else:
            body = _('Card payment reversed via AFS Void #%s (confirmed by the terminal; '
                     'AFS returned no auth code / RRN for it).') % self.id
        invoice.sudo().message_post(body=body)
        return True

    def _reverse_payment_and_reconcile(self):
        self.ensure_one()
        # Accounting actions run with the user's own rights; the superuser
        # (cron, upgrade) keeps its own environment.
        user_env = self.env if self.env.uid == SUPERUSER_ID else self.env(su=False)
        sale = self.reverses_id
        payment = user_env['account.payment'].browse(sale.payment_id.id)
        if payment and payment.state != 'canceled':
            reconciled_lines = payment.move_id.line_ids.filtered(
                lambda l: l.account_id.reconcile and (l.matched_debit_ids or l.matched_credit_ids)
            )
            if reconciled_lines:
                reconciled_lines.remove_move_reconcile()
            # Odoo 18: resets a posted payment entry to draft and cancels it;
            # also handles payments without a journal entry.
            payment.action_cancel()
        invoice = user_env['account.move'].browse(sale.move_id.id)
        credit_note = user_env['account.move'].browse(self.credit_note_id.id)
        if credit_note and credit_note.state == 'posted':
            open_lines = (invoice.line_ids | credit_note.line_ids).filtered(
                lambda l: l.account_id.account_type in ('asset_receivable', 'liability_payable')
                and not l.reconciled
            )
            for account in open_lines.account_id:
                lines = open_lines.filtered(lambda l: l.account_id == account)
                if len(lines) > 1:
                    lines.reconcile()

    @api.depends('operation', 'state', 'reverses_id.reversed_by_id', 'reverses_id.payment_id.state')
    def _compute_reversal_incomplete(self):
        for txn in self:
            sale = txn.reverses_id
            txn.reversal_incomplete = bool(
                txn.operation == 'void' and txn.state == 'approved' and sale and sale.operation == 'sale'
                and (sale.reversed_by_id != txn or (sale.payment_id and sale.payment_id.state != 'canceled'))
            )

    def action_complete_reversal(self):
        """Recovery: finish the bookkeeping of an approved Void that did not
        complete on its own (worker died, lock date, ...)."""
        self.ensure_one()
        check_afs_manager(self.env)
        self.sudo()._complete_reversal()

    # ------------------------------------------------------------------
    # Shared result handling
    # ------------------------------------------------------------------

    @api.model
    def _safe_finalize_by_id(self, txn_id, vals, *, late_reason):
        """Race-safe write of a terminal result onto a 'pending' afs.transaction.

        Returns True if it wrote to the original record. If the record was
        already finalized by something else first (most commonly: the user
        clicked Cancel Payment while this response was in flight), the result
        is NEVER silently dropped — it's recorded as a new transaction linked
        to the original via reverses_id, and this returns False so the caller
        can react (e.g. flag a late approval loudly, since that means real
        money moved despite the cancellation). With late_reason=None nothing
        is recorded in that case (used for non-outcome bookkeeping).

        Race-safety: `SELECT ... FOR NO KEY UPDATE` takes the same row lock a
        plain UPDATE would (e.g. from action_request_cancellation's own
        write), so the two can never both "win". Under Odoo's REPEATABLE READ
        isolation the loser of that race gets a serialization failure rather
        than a stale read, so retry on a fresh cursor instead of losing the
        outcome.
        """
        for attempt in range(3):
            try:
                with self.env.registry.cursor() as new_cr:
                    new_env = self.env(cr=new_cr, su=True)
                    new_cr.execute(
                        "SELECT id FROM afs_transaction WHERE id = %s AND state = 'pending' FOR NO KEY UPDATE",
                        (txn_id,))
                    if not new_cr.fetchone():
                        break
                    new_env['afs.transaction'].browse(txn_id).write(vals)
                return True
            except psycopg2.errors.SerializationFailure:
                if attempt == 2:
                    raise
                _logger.info('AFS: serialization conflict finalizing transaction %s, retrying', txn_id)

        if late_reason is None:
            return False
        with self.env.registry.cursor() as new_cr:
            new_env = self.env(cr=new_cr, su=True)
            original = new_env['afs.transaction'].browse(txn_id)
            late_vals = dict(vals)
            late_vals.update({
                'move_id': original.move_id.id,
                'credit_note_id': original.credit_note_id.id,
                'terminal_id': original.terminal_id.id,
                'company_id': original.company_id.id,
                'operation': original.operation,
                'reverses_id': txn_id,
                'requested_amount': original.requested_amount,
                'currency_id': original.currency_id.id,
                'our_reference': original.our_reference,
                'request_xml': original.request_xml,
            })
            late_vals['error_message'] = '\n'.join(filter(None, [
                _(
                    'This response arrived after %s (original afs.transaction #%s). Recorded '
                    'separately so the outcome is never silently lost.'
                ) % (late_reason, txn_id),
                vals.get('error_message'),
            ]))
            new_env['afs.transaction'].create(late_vals)
        return False

    # ------------------------------------------------------------------
    # Cancellation / housekeeping
    # ------------------------------------------------------------------

    def action_request_cancellation(self):
        """For when the client walks away and the terminal is still
        prompting: asks AFS to stop the pending Sale/Void.

        The record deliberately stays 'pending': the in-flight call is still
        open and AFS answers it within seconds — "Cancelled By ECR" (card not
        charged) finalises THIS record as 'cancelled'; if the terminal had
        already completed the payment, the approval is recorded here as well,
        with a warning. One record per attempt, no guessing. If the worker
        died and no answer ever comes, the stale-pending cron flags it
        'unknown' as for any other lost call."""
        self.ensure_one()
        check_afs_user(self.env)
        if self.operation == 'void':
            check_afs_manager(self.env)
        if self.state != 'pending':
            raise UserError(_('Request Cancellation only applies to a pending transaction.'))
        if self.cancel_requested_at:
            raise UserError(_('Cancellation was already requested; waiting for the terminal to answer.'))
        terminal = self.terminal_id
        config = terminal._get_config()
        client = terminal._get_client()
        request_xml = afs_client.build_cancellation_envelope(config)
        try:
            result = terminal._parse_response(client.post('RequestCancellation', request_xml))
        except Exception as exc:  # noqa: BLE001
            raise UserError(_('Request Cancellation failed: %s') % exc)

        # Bookkeeping on a pending record — not an outcome, so no new row.
        self.sudo()._safe_finalize_by_id(self.id, {
            'cancel_requested_at': fields.Datetime.now(),
            'cancel_response_status': ' — '.join(filter(None, [
                result.web_response_status, result.web_response_error])),
            'cancel_response_xml': result.response_xml,
        }, late_reason=None)

    def _fresh_cancel_requested(self):
        """Read cancel_requested_at past this cursor's snapshot: it may have
        been committed by the user's Cancel request after this thread began."""
        self.ensure_one()
        with self.env.registry.cursor() as cr:
            cr.execute('SELECT cancel_requested_at FROM afs_transaction WHERE id = %s', (self.id,))
            row = cr.fetchone()
        return bool(row and row[0])

    def _cron_sweep_stale_pending(self):
        """Safety net for the async Sale/Void flow: a background thread can die
        with its worker (redeploy, crash) without ever finalising its
        transaction. Anything still 'pending' well past its own terminal's
        timeout is not going to resolve on its own — flag it the same way a
        dropped connection with no recoverable outcome would be flagged."""
        now = fields.Datetime.now()
        for txn in self.sudo().search([('state', '=', 'pending'), ('started_at', '!=', False)]):
            if txn.started_at <= now - timedelta(seconds=txn.terminal_id._in_flight_window()):
                txn.write({
                    'state': 'unknown',
                    'error_message': _(
                        'No response was received within the expected window; the '
                        'background worker may have been interrupted. Outcome unknown '
                        '— do not retry. Verify on the terminal or AFS portal.'
                    ),
                })


def _new_reference():
    """Short per-attempt token sent as Printer/InvoiceNumber and
    ReferenceNumber — what EnquiryByRef later looks the attempt up by."""
    return uuid.uuid4().hex[:8].upper()


def _post_enquiry_by_ref(client, config, reference):
    """EnquiryByRef for one of our own references (spec §7: "based on original
    ECR reference number originated and sent by ECR") — a direct lookup of
    that exact attempt. Returns the raw response XML; transport errors
    propagate."""
    envelope = afs_client.build_enquiry_by_ref_envelope(
        config, orig_auth_code='', orig_invoice_number=reference, orig_rrn='',
        printer=afs_client.AfsPrinter(invoice_number=reference, reference_number=reference))
    return client.post('EnquiryByRef', envelope)


def _afs_has_record(result):
    """Something came back for our reference at all (vs. AFS having no record of it)."""
    return bool(result.pos_rrn or result.pos_auth_code or result.pos_resp_code)


def _outcome_unknown(exc):
    """The request may have reached the terminal but its answer was lost:
    a timeout, a dropped connection, or a 5xx from the gateway in front of AFS
    (seen live: 520/524 — the proxy gave up waiting on AFS, which may still
    have processed the payment). Never "not charged" — recover by reference."""
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    response = getattr(exc, 'response', None)
    return isinstance(exc, requests.HTTPError) and response is not None and response.status_code >= 500


def _postcommit_start_void(dbname, uid, context, sale_txn_id, credit_note_id):
    """Starts the Void for a credit note only once its posting has actually
    committed: the credit note is never half-posted, and a rollback of the
    posting can never leave a real refund behind."""
    registry = odoo.modules.registry.Registry(dbname)
    try:
        with registry.cursor() as cr:
            env = api.Environment(cr, uid, context)
            sale = env['afs.transaction'].sudo().browse(sale_txn_id)
            credit_note = env['account.move'].browse(credit_note_id)
            try:
                sale._start_void(credit_note=credit_note)
            except Exception as exc:  # noqa: BLE001
                _logger.exception('AFS: could not start Void for credit note %s', credit_note_id)
                credit_note.sudo().message_post(body=env._(
                    'The card refund (Void) could not be started: %s. The credit note is posted but '
                    'the card has NOT been refunded — use "Direct Void" on the AFS transaction once '
                    'the terminal is free, or refund through AFS.'
                ) % exc)
    except Exception:  # noqa: BLE001
        _logger.exception('AFS: postcommit Void start failed for credit note %s', credit_note_id)


def schedule_void_after_commit(env, sale_txn, credit_note):
    if env.registry.in_test_mode():
        # TestCursor drops post-commit hooks; run it now instead (the test
        # cursor's data is visible to the fresh cursors once flushed).
        env.flush_all()
        sale_txn.sudo()._start_void(credit_note=credit_note)
        return
    env.cr.postcommit.add(functools.partial(
        _postcommit_start_void, env.cr.dbname, env.uid, dict(env.context), sale_txn.id, credit_note.id))
