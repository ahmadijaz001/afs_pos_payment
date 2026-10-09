# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError

from ..models.afs_terminal import check_afs_user


class AfsPaymentWizard(models.TransientModel):
    _name = 'afs.payment.wizard'
    _description = 'Take a Card Payment via AFS'

    move_id = fields.Many2one('account.move', string='Invoice', required=True)
    company_id = fields.Many2one(related='move_id.company_id')
    terminal_id = fields.Many2one(
        'afs.terminal', string='Terminal', required=True,
        domain="[('company_id', '=', company_id), '|', ('user_ids', '=', False), ('user_ids', 'in', uid)]",
    )
    currency_id = fields.Many2one(related='move_id.currency_id', readonly=True)
    amount = fields.Monetary(currency_field='currency_id', required=True)

    @api.onchange('move_id')
    def _onchange_move_id(self):
        if self.move_id:
            self.amount = self.move_id.amount_residual

    @api.constrains('amount')
    def _check_amount(self):
        for wizard in self:
            wizard._validate_amount()

    def _validate_amount(self):
        self.ensure_one()
        if self.amount <= 0:
            raise UserError(_('The amount to charge must be greater than zero.'))
        if self.move_id and self.currency_id.compare_amounts(self.amount, self.move_id.amount_residual) > 0:
            raise UserError(_('The amount to charge cannot exceed the invoice residual (%s).')
                            % self.move_id.amount_residual)
        if self.terminal_id and self.terminal_id.max_amount and self.amount > self.terminal_id.max_amount:
            raise UserError(_('The amount exceeds this terminal\'s per-transaction limit (%s).')
                            % self.terminal_id.max_amount)

    def _validate(self):
        """Everything is re-checked here, server-side: the wizard can be created
        over RPC with any values, so what action_afs_pay checked when it opened
        the wizard proves nothing."""
        self.ensure_one()
        check_afs_user(self.env)
        move = self.move_id
        terminal = self.terminal_id
        if move.move_type not in ('out_invoice', 'out_receipt'):
            raise UserError(_('Card payment is only available for customer invoices and receipts.'))
        if move.state != 'posted':
            raise UserError(_('The invoice must be posted before taking a card payment.'))
        if move.payment_state not in ('not_paid', 'partial'):
            raise UserError(_('This invoice is already paid.'))
        if not terminal.active:
            raise UserError(_('Terminal "%s" is archived.') % terminal.name)
        if terminal.company_id != move.company_id:
            raise UserError(_('Terminal "%s" belongs to another company than this invoice.') % terminal.name)
        terminal._check_user_allowed()
        if move.currency_id != terminal.currency_id:
            raise UserError(_(
                'This invoice is in %s but terminal "%s" is configured for %s. '
                'Never charge a card in a currency the terminal was not set up for.'
            ) % (move.currency_id.name, terminal.name, terminal.currency_id.name))
        self._validate_amount()

    def action_confirm(self):
        """Starts the Sale and returns immediately. The invoice reopens showing
        afs_state='pending'; the terminal wait and the eventual outcome happen
        in a background thread (afs.transaction._run_sale_background) and are
        pushed to the browser over the bus."""
        self.ensure_one()
        self._validate()
        self.env['afs.transaction']._start_sale(self.move_id, self.terminal_id.sudo(), self.amount)
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'res_id': self.move_id.id,
            'view_mode': 'form',
            'target': 'current',
        }
