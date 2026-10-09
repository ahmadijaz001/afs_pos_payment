# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError

from .afs_terminal import check_afs_manager, check_afs_user
from .afs_transaction import schedule_void_after_commit


class AccountMove(models.Model):
    _inherit = 'account.move'

    afs_terminal_id = fields.Many2one(
        'afs.terminal', string='AFS Terminal',
        domain="[('company_id', '=', company_id)]",
        default=lambda self: self._default_afs_terminal_id(),
    )
    afs_transaction_ids = fields.One2many('afs.transaction', 'move_id', string='AFS Transactions')
    afs_transaction_count = fields.Integer(compute='_compute_afs_transaction_count', compute_sudo=True)
    afs_void_on_post = fields.Boolean(
        string='Void Card Payment on Post', copy=False,
        help='Set by "Reverse Card Payment". When this credit note is posted, the '
             "original invoice's card payment is refunded to the card (AFS Void). "
             'Only AFS managers can post a credit note with this ticked, and only for '
             'the full card amount. Leave it unticked for corrections and re-issues: '
             'the card is then never touched.',
    )
    afs_state = fields.Selection([
        ('none', 'No Card Payment'),
        ('pending', 'Pending'),
        ('paid', 'Paid by Card'),
        ('declined', 'Declined'),
        ('unknown', 'Unknown'),
        ('reversing', 'Reversing'),
        ('reversed', 'Reversed'),
    ], compute='_compute_afs_state', store=True, default='none')

    # Full snapshot of the latest approved sale, kept on the invoice so it is
    # searchable/filterable/exportable without joining afs.transaction.
    afs_auth_code = fields.Char(compute='_compute_afs_snapshot', store=True)
    afs_rrn = fields.Char(compute='_compute_afs_snapshot', store=True)
    afs_stan = fields.Char(compute='_compute_afs_snapshot', store=True)
    afs_pos_invoice_number = fields.Char(compute='_compute_afs_snapshot', store=True)
    afs_batch_number = fields.Char(compute='_compute_afs_snapshot', store=True)
    afs_captured_amount = fields.Monetary(compute='_compute_afs_snapshot', store=True)
    afs_currency_code = fields.Char(compute='_compute_afs_snapshot', store=True, help='ISO 4217 numeric code as returned by the terminal.')
    afs_card_pan = fields.Char(string='Card (masked)', compute='_compute_afs_snapshot', store=True)
    afs_issuer = fields.Char(compute='_compute_afs_snapshot', store=True)
    afs_scheme = fields.Char(compute='_compute_afs_snapshot', store=True)
    afs_emv_aid = fields.Char(compute='_compute_afs_snapshot', store=True)
    afs_emv_tvr = fields.Char(compute='_compute_afs_snapshot', store=True)
    afs_emv_cryptogram = fields.Char(compute='_compute_afs_snapshot', store=True)
    afs_entry_mode = fields.Char(compute='_compute_afs_snapshot', store=True)
    afs_cvm_id = fields.Char(compute='_compute_afs_snapshot', store=True)
    afs_txn_datetime = fields.Datetime(compute='_compute_afs_snapshot', store=True)
    afs_txn_name = fields.Char(compute='_compute_afs_snapshot', store=True)
    afs_resp_code = fields.Char(compute='_compute_afs_snapshot', store=True)
    afs_resp_text = fields.Char(compute='_compute_afs_snapshot', store=True)
    afs_resp_status = fields.Char(compute='_compute_afs_snapshot', store=True, help='-1 unknown / 0 declined / 1 approved.')
    afs_receipt = fields.Text(compute='_compute_afs_snapshot', store=True)
    afs_terminal_tid = fields.Char(compute='_compute_afs_snapshot', store=True, help='Snapshot of the TID at the time of sale.')
    afs_terminal_mid = fields.Char(compute='_compute_afs_snapshot', store=True, help='Snapshot of the MID at the time of sale.')

    def _default_afs_terminal_id(self):
        # sudo: this default runs for every new invoice, including for users
        # with no AFS access at all, who must still be able to create invoices.
        terminals = self.env['afs.terminal'].sudo().search([('company_id', '=', self.env.company.id)])
        allowed = terminals.filtered(lambda t: not t.user_ids or self.env.user in t.user_ids)
        default_id = self.env['ir.config_parameter'].sudo().get_param('afs_pos_integration.default_terminal_id')
        preferred = allowed.filtered(lambda t: str(t.id) == default_id)
        return (preferred or allowed)[:1]

    @api.depends('afs_transaction_ids')
    def _compute_afs_transaction_count(self):
        for move in self:
            move.afs_transaction_count = len(move.afs_transaction_ids)

    @api.depends(
        'afs_transaction_ids.state', 'afs_transaction_ids.operation',
        'afs_transaction_ids.reversed_by_id', 'afs_transaction_ids.reverses_id',
    )
    def _compute_afs_state(self):
        for move in self:
            # A late, non-approved response recorded against an earlier attempt
            # (reverses_id set) is not a new attempt: no money moved, so it must
            # not decide the invoice's status.
            sales = move.afs_transaction_ids.filtered(
                lambda t: t.operation == 'sale' and not (t.reverses_id and t.state != 'approved')
            ).sorted(key=lambda t: t.create_date or fields.Datetime.now(), reverse=True)
            if not sales:
                move.afs_state = 'none'
                continue
            latest = sales[0]
            if latest.state == 'pending':
                move.afs_state = 'pending'
            elif latest.state == 'declined':
                move.afs_state = 'declined'
            elif latest.state == 'cancelled':
                move.afs_state = 'none'
            elif latest.state == 'unknown':
                move.afs_state = 'unknown'
            elif latest.state == 'error':
                # A Sale only ends in 'error' when it provably never reached
                # the card (request refused before the terminal, or resolved
                # "not processed" by a manager) — real uncertainty is 'unknown'.
                move.afs_state = 'none'
            elif latest.state == 'approved':
                if latest.reversed_by_id:
                    move.afs_state = 'reversed'
                elif move.afs_transaction_ids.filtered(
                        lambda t: t.operation == 'void' and t.reverses_id == latest and t.state == 'pending'):
                    move.afs_state = 'reversing'
                else:
                    move.afs_state = 'paid'
            else:
                move.afs_state = 'none'

    @api.depends(
        'afs_transaction_ids.state', 'afs_transaction_ids.operation',
        'afs_transaction_ids.pos_auth_code', 'afs_transaction_ids.pos_rrn',
    )
    def _compute_afs_snapshot(self):
        snapshot_fields = [
            ('afs_auth_code', 'pos_auth_code'),
            ('afs_rrn', 'pos_rrn'),
            ('afs_stan', 'pos_stan'),
            ('afs_pos_invoice_number', 'pos_invoice_number'),
            ('afs_batch_number', 'pos_batch_number'),
            ('afs_captured_amount', 'pos_amount'),
            ('afs_currency_code', 'pos_currency_code'),
            ('afs_card_pan', 'pos_pan'),
            ('afs_issuer', 'pos_issuer_name'),
            ('afs_scheme', 'pos_scheme'),
            ('afs_emv_aid', 'pos_emv_aid'),
            ('afs_emv_tvr', 'pos_emv_tvr'),
            ('afs_emv_cryptogram', 'pos_emv_cryptogram'),
            ('afs_entry_mode', 'pos_entry_mode'),
            ('afs_cvm_id', 'pos_cvm_id'),
            ('afs_txn_datetime', 'pos_datetime'),
            ('afs_txn_name', 'pos_txn_name'),
            ('afs_resp_code', 'pos_resp_code'),
            ('afs_resp_text', 'pos_resp_text'),
            ('afs_resp_status', 'pos_resp_status'),
            ('afs_receipt', 'pos_receipt'),
        ]
        for move in self:
            sales = move.afs_transaction_ids.filtered(
                lambda t: t.operation == 'sale' and t.state == 'approved'
            ).sorted(key=lambda t: t.create_date or fields.Datetime.now(), reverse=True)
            latest = sales[:1]
            if latest:
                for move_field, txn_field in snapshot_fields:
                    move[move_field] = latest[txn_field]
                move.afs_terminal_tid = latest.terminal_id.tid
                move.afs_terminal_mid = latest.terminal_id.mid
            else:
                for move_field, _txn_field in snapshot_fields:
                    move[move_field] = False
                move.afs_terminal_tid = False
                move.afs_terminal_mid = False

    def action_afs_pay(self):
        self.ensure_one()
        check_afs_user(self.env)
        if self.move_type not in ('out_invoice', 'out_receipt'):
            raise UserError(_('Card payment is only available for customer invoices and receipts.'))
        if self.state != 'posted':
            raise UserError(_('The invoice must be posted before taking a card payment.'))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Pay by Card'),
            'res_model': 'afs.payment.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_move_id': self.id,
                'default_terminal_id': self.afs_terminal_id.id,
                'default_amount': self.amount_residual,
            },
        }

    def action_view_afs_transactions(self):
        self.ensure_one()
        check_afs_user(self.env)
        return {
            'type': 'ir.actions.act_window',
            'name': _('AFS Transactions'),
            'res_model': 'afs.transaction',
            'view_mode': 'list,form',
            'domain': [('move_id', '=', self.id)],
            'context': {'default_move_id': self.id},
        }

    def action_afs_cancel_pending(self):
        """Called by the invoice's pending-payment widget while a Sale or a
        Void is still waiting on the terminal — the only useful place to
        actually cancel it, since the flow that started it already returned.
        Note: per AFS, RequestCancellation only stops a request before it
        reaches the terminal — once it's already there prompting, this
        cleans up our own side but may not stop the physical device."""
        self.ensure_one()
        check_afs_user(self.env)
        pending = self.afs_transaction_ids.filtered(
            lambda t: t.operation in ('sale', 'void') and t.state == 'pending'
        ).sorted(key=lambda t: t.create_date or fields.Datetime.now(), reverse=True)
        if not pending:
            return
        pending[0].action_request_cancellation()

    def action_afs_reverse(self):
        """Reversing a card payment goes through a Credit Note, not a direct
        Void button: this opens the standard Odoo reversal wizard for this
        invoice, with the resulting credit note flagged afs_void_on_post. Once
        that credit note is posted, _post below calls Void automatically.
        Credit notes created any other way are never flagged, so corrections
        and re-issues never refund a card by accident.
        This button disappears once afs_state is 'reversed' (view domain)."""
        self.ensure_one()
        check_afs_manager(self.env)
        sales = self.afs_transaction_ids.filtered(
            lambda t: t.operation == 'sale' and t.state == 'approved' and not t.reversed_by_id
        )
        if not sales:
            raise UserError(_('No approved card payment on this invoice to reverse.'))
        for sale in sales.sudo():
            sale._check_void_delay()
        action = self.env['ir.actions.actions']._for_xml_id('account.action_view_account_move_reversal')
        action['name'] = _('Reverse Card Payment (Credit Note)')
        action['context'] = {
            'active_model': 'account.move',
            'active_ids': self.ids,
            'active_id': self.id,
            'afs_void_on_post': True,
        }
        return action

    def _reverse_moves(self, default_values_list=None, cancel=False):
        # Flag the credit note at creation, not afterwards: with cancel=True
        # (reverse-and-reconcile) core posts it inside this very call.
        if self.env.context.get('afs_void_on_post'):
            default_values_list = default_values_list or [{} for _move in self]
            default_values_list = [
                dict(vals, afs_void_on_post=True) if move.move_type in ('out_invoice', 'out_receipt') else vals
                for move, vals in zip(self, default_values_list)
            ]
        return super()._reverse_moves(default_values_list=default_values_list, cancel=cancel)

    def _post(self, soft=True):
        # _post, not action_post: also covers credit notes posted by core
        # itself (reverse-and-reconcile, auto-post).
        posted = super()._post(soft=soft)
        for move in posted.filtered(lambda m: m.move_type == 'out_refund' and m.reversed_entry_id and m.afs_void_on_post):
            move._afs_auto_void_credit_note()
        return posted

    def _afs_auto_void_credit_note(self):
        """Posting a credit note flagged afs_void_on_post refunds the original
        card payment with Void. Everything that can be checked is checked here,
        so a refusal blocks the posting itself; the Void then starts only once
        the posting has committed (see schedule_void_after_commit).
        The settings toggle stays as a global kill switch."""
        self.ensure_one()
        enabled = self.env['ir.config_parameter'].sudo().get_param(
            'afs_pos_integration.auto_void_on_credit_note', 'true')
        if enabled != 'true':
            return
        check_afs_manager(self.env)
        original = self.reversed_entry_id
        sales = original.sudo().afs_transaction_ids.filtered(
            lambda t: t.operation == 'sale' and t.state == 'approved' and not t.reversed_by_id
        ).sorted(key=lambda t: t.create_date or fields.Datetime.now(), reverse=True)
        if not sales:
            raise UserError(_(
                'This credit note is set to void a card payment, but invoice %s has no '
                'approved, unreversed card payment. Untick "Void Card Payment on Post".'
            ) % original.name)
        sale = sales[0]
        if sale.currency_id.compare_amounts(abs(self.amount_total), sale.pos_amount) != 0:
            raise UserError(_(
                'AFS supports full-amount reversals only (this credit note is %s, the '
                'original card payment was %s). Untick "Void Card Payment on Post" and '
                'process the partial refund on the terminal or through AFS.'
            ) % (self.amount_total, sale.pos_amount))
        sale._check_voidable()
        schedule_void_after_commit(self.env, sale, self)
