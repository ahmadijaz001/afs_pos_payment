# -*- coding: utf-8 -*-
from odoo import api, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    afs_default_terminal_id = fields.Many2one(
        'afs.terminal', string='Default AFS Terminal',
        config_parameter='afs_pos_integration.default_terminal_id',
    )
    # Not a plain config_parameter=... Boolean: Odoo's ir.config_parameter storage
    # deletes the row on a False write, which would make "turned off" indistinguishable
    # from "never configured" and silently fall back to the default. Store an explicit
    # 'true'/'false' string instead so an intentional "off" actually sticks.
    afs_auto_void_on_credit_note = fields.Boolean(
        string='Auto-Void on Credit Note Post',
        compute='_compute_afs_auto_void_on_credit_note',
        inverse='_inverse_afs_auto_void_on_credit_note',
        readonly=False,
        help='This is the standard way a card payment is reversed: the invoice\'s '
             '"Reverse Card Payment" button creates a credit note, and posting that '
             'credit note calls Void automatically. On by default. Switch off only if '
             'you want credit notes handled without ever touching the card terminal.',
    )
    afs_terminal_param_hint = fields.Text(
        string='Configured Terminals — Secure Key Parameters',
        compute='_compute_afs_terminal_param_hint',
    )

    def _compute_afs_auto_void_on_credit_note(self):
        value = self.env['ir.config_parameter'].sudo().get_param(
            'afs_pos_integration.auto_void_on_credit_note', 'true')
        for rec in self:
            rec.afs_auto_void_on_credit_note = (value == 'true')

    def _inverse_afs_auto_void_on_credit_note(self):
        icp = self.env['ir.config_parameter'].sudo()
        for rec in self:
            icp.set_param('afs_pos_integration.auto_void_on_credit_note',
                           'true' if rec.afs_auto_void_on_credit_note else 'false')

    @api.depends_context('uid')
    def _compute_afs_terminal_param_hint(self):
        terminals = self.env['afs.terminal'].search([])
        icp = self.env['ir.config_parameter'].sudo()
        for rec in self:
            if not terminals:
                rec.afs_terminal_param_hint = ''
                continue
            lines = []
            for terminal in terminals:
                has_value = bool(icp.get_param(terminal.secure_key_param))
                lines.append('%s: %s [%s]' % (
                    terminal.name, terminal.secure_key_param,
                    'set' if has_value else 'MISSING',
                ))
            rec.afs_terminal_param_hint = '\n'.join(lines)
