# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError

from ..models.afs_terminal import check_afs_manager


class AfsResolveWizard(models.TransientModel):
    """Manual resolution of a Sale/Void whose outcome is Unknown (the
    connection or AFS's gateway dropped the answer and EnquiryByRef found
    nothing). A manager checks the terminal receipt / AFS portal and records
    what really happened; the module then does the bookkeeping it would have
    done itself. Everything entered is kept on the transaction."""
    _name = 'afs.resolve.wizard'
    _description = 'Resolve an Unknown AFS Transaction'

    transaction_id = fields.Many2one('afs.transaction', required=True, readonly=True)
    operation = fields.Selection(related='transaction_id.operation')
    outcome = fields.Selection([
        ('approved', 'It went through (verified on the receipt / AFS portal)'),
        ('not_processed', 'It did not go through — nothing was charged / refunded'),
    ], required=True)
    pos_auth_code = fields.Char(string='Auth Code', help='From the terminal receipt or AFS portal.')
    pos_rrn = fields.Char(string='RRN', help='From the terminal receipt or AFS portal.')
    note = fields.Text(required=True, help='How this was verified (receipt, AFS portal, AFS support ticket...).')

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        if self.env.context.get('active_model') == 'afs.transaction' and 'transaction_id' in fields_list:
            res['transaction_id'] = self.env.context.get('active_id')
        return res

    def action_resolve(self):
        self.ensure_one()
        check_afs_manager(self.env)
        txn = self.transaction_id.sudo()
        if txn.state != 'unknown' or txn.operation not in ('sale', 'void'):
            raise UserError(_('Only a Sale or Void in state Unknown can be resolved manually.'))
        stamp = _('Resolved manually by %(user)s on %(date)s: %(note)s',
                  user=self.env.user.name, date=fields.Datetime.now(), note=self.note)
        error_message = '\n'.join(filter(None, [txn.error_message, stamp]))
        txn = txn.with_context(afs_manual_resolution=True)

        if self.outcome == 'not_processed':
            txn.write({'state': 'error', 'error_message': error_message})
            txn.move_id.sudo().message_post(body=_(
                'AFS %(op)s #%(id)s resolved as NOT processed. %(note)s',
                op=txn.operation, id=txn.id, note=self.note))
            return {'type': 'ir.actions.act_window_close'}

        if not (self.pos_auth_code and self.pos_rrn):
            raise UserError(_('Enter the auth code and RRN from the receipt / AFS portal.'))
        txn._mark_approved_after_unknown({
            'pos_auth_code': self.pos_auth_code,
            'pos_rrn': self.pos_rrn,
            'error_message': error_message,
        })
        if txn.operation == 'sale':
            self._register_resolved_payment(txn)
        return {'type': 'ir.actions.act_window_close'}

    def _register_resolved_payment(self, txn):
        move = self.env['account.move'].browse(txn.move_id.id)  # user's own accounting rights
        payment = self.env['afs.transaction']._create_card_payment(
            move, txn.terminal_id.journal_id, txn.requested_amount,
            'AFS %s/%s (resolved manually)' % (self.pos_auth_code, self.pos_rrn))
        txn.write({'payment_id': payment.id})
        move.sudo().message_post(body=_(
            'AFS Sale #%(id)s resolved as approved (auth %(auth)s / RRN %(rrn)s); payment registered. %(note)s',
            id=txn.id, auth=self.pos_auth_code, rrn=self.pos_rrn, note=self.note))
