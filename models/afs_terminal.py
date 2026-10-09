# -*- coding: utf-8 -*-
import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError

from ..services import afs_client

_logger = logging.getLogger(__name__)


class AfsTerminal(models.Model):
    _name = 'afs.terminal'
    _inherit = ['mail.thread']
    _description = 'AFS Payment Terminal'
    _order = 'name'

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)

    mid = fields.Char(string='Merchant ID (MID)', required=True)
    tid = fields.Char(string='Terminal ID (TID)', required=True)
    tenant = fields.Char(required=True)
    secure_key_param = fields.Char(
        string='Secure Key Parameter',
        required=True,
        help="Name of the ir.config_parameter key holding this terminal's "
             "MerchantSecureKey, e.g. 'afs_pos.secure_key.reception_dubai'. "
             "The key itself is never stored on this record.",
    )
    endpoint_url = fields.Char(
        required=True,
        default='https://ereceipt.afs.com.bh/Ecr.Ae.Afs/EcrComInterface.svc',
    )
    integrator_name = fields.Char(default='RAALC')

    currency_id = fields.Many2one('res.currency', string='Settlement Currency', required=True,
                                   default=lambda self: self.env.company.currency_id)
    journal_id = fields.Many2one(
        'account.journal', string='Clearing Journal', required=True,
        domain="[('type', 'in', ('bank', 'cash')), ('company_id', '=', company_id)]",
        help='Dedicated card-clearing journal. AFS settles net of fees as one '
             'lump sum, so individual card payments will not match a bank '
             'statement line one-to-one; use a separate journal, not the main bank journal.',
    )

    teller_full_name = fields.Char(string='Teller Full Name (default)')
    teller_user_name = fields.Char(string='Teller Username (default)')
    printer_width = fields.Integer(default=32)
    # Not booleans — the terminal's Printer contract defines these as enums (spec §8.2).
    print_pos_receipt = fields.Selection([
        ('0', 'None'),
        ('1', 'Merchant Copy'),
        ('2', 'Customer Copy'),
        ('3', 'Both Copies'),
    ], string='Print POS Receipt', default='3', required=True)
    print_receipt_note = fields.Selection([
        ('0', 'None'),
        ('1', 'Before Header'),
        ('2', 'After Header'),
        ('3', 'Before Footer'),
        ('4', 'After Footer'),
    ], string='Print Receipt Note', default='0', required=True)

    timeout = fields.Integer(default=180, help='Seconds to wait for the cardholder to complete the transaction.')
    max_amount = fields.Monetary(currency_field='currency_id', help='Per-transaction ceiling. 0 = no limit.')

    user_ids = fields.Many2many('res.users', string='Restricted To Users',
                                 help='Leave empty to allow any AFS user to use this terminal.')

    void_requires_open_batch = fields.Boolean(
        string='Void Requires Open Batch', default=True,
        help='Refuse Void once a Settlement has run on this terminal since the '
             'sale being reversed. Switch off only after confirming with AFS '
             'that Void succeeds across settled batches.',
    )
    last_settlement_batch_number = fields.Char(
        readonly=True,
        help="AFS's Settlement response does not include a batch number "
             '(spec §8.9) — this stays blank unless AFS ever sends one.',
    )
    last_settlement_date = fields.Datetime(
        readonly=True, help='Set when a Settlement call is actually approved by AFS.')

    transaction_ids = fields.One2many('afs.transaction', 'terminal_id', string='Transactions')
    transaction_count = fields.Integer(compute='_compute_transaction_count')

    _sql_constraints = [
        ('mid_tid_company_uniq', 'unique(mid, tid, company_id)',
         'A terminal with this MID/TID already exists for this company.'),
    ]

    @api.depends('transaction_ids')
    def _compute_transaction_count(self):
        counts = self.env['afs.transaction']._read_group(
            [('terminal_id', 'in', self.ids)], ['terminal_id'], ['__count'],
        )
        by_terminal = {terminal.id: count for terminal, count in counts}
        for terminal in self:
            terminal.transaction_count = by_terminal.get(terminal.id, 0)

    def _get_secure_key(self):
        self.ensure_one()
        if not self.secure_key_param:
            raise UserError(_('Terminal "%s" has no secure key parameter configured.') % self.name)
        key = self.env['ir.config_parameter'].sudo().get_param(self.secure_key_param)
        if not key:
            raise UserError(_(
                'No value found for system parameter "%s". Ask an administrator '
                'to set the MerchantSecureKey for terminal "%s".'
            ) % (self.secure_key_param, self.name))
        return key

    def _get_config(self, teller_full_name=None, teller_user_name=None):
        self.ensure_one()
        return afs_client.AfsConfig(
            ecr_currency_code=self._currency_numeric_code(),
            ecr_tiller_full_name=teller_full_name or self.teller_full_name or self.env.user.name,
            ecr_tiller_user_name=teller_user_name or self.teller_user_name or self.env.user.login,
            integrator_name=self.integrator_name or 'RAALC',
            merchant_secure_key=self._get_secure_key(),
            mid=self.mid,
            tenant=self.tenant,
            tid=self.tid,
        )

    def _currency_numeric_code(self):
        self.ensure_one()
        numeric = {v: k for k, v in afs_client.CURRENCY_NUMERIC_TO_ALPHA.items()}.get(self.currency_id.name)
        if not numeric:
            raise UserError(_(
                'Currency %s on terminal "%s" has no known ISO 4217 numeric code configured in this module.'
            ) % (self.currency_id.name, self.name))
        return numeric

    def _get_client(self):
        self.ensure_one()
        return afs_client.AfsClient(endpoint_url=self.endpoint_url, timeout=self.timeout or 180)

    def action_view_transactions(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Transactions'),
            'res_model': 'afs.transaction',
            'view_mode': 'list,form',
            'domain': [('terminal_id', '=', self.id)],
            'context': {'default_terminal_id': self.id},
        }

    def action_test_connection(self):
        """Uses RequestCancellation, not Enquiry: Enquiry requires
        OrigInvoiceNumber/OrigRrn (spec §8.12, both mandatory), so a blank
        Enquiry is rejected outright regardless of whether credentials are
        correct — it can never usefully test connectivity. RequestCancellation
        needs only Config, returns a clean Success when nothing is in-flight
        (confirmed live), and still fails clearly (e.g. "Invalid Secure Key",
        "Unknown Merchant") when credentials are wrong."""
        self.ensure_one()
        client = self._get_client()
        config = self._get_config()
        try:
            _envelope, _response_xml, result = client.cancel(config)
        except Exception as exc:  # noqa: BLE001 - surface any transport/parse error to the user
            raise UserError(_('Connection test failed: %s') % exc)

        ok = result.web_response_status.strip().lower().startswith('success')
        message = (
            _('Connection OK — credentials accepted.')
            if ok else
            _('Connection failed: %s') % (result.web_response_error or result.web_response_status or _('no response'))
        )
        self.message_post(body=message)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('AFS Test Connection'),
                'message': message,
                'sticky': False,
                'type': 'success' if ok else 'danger',
            },
        }

    def action_run_settlement(self):
        self.ensure_one()
        client = self._get_client()
        config = self._get_config()
        request_xml, response_xml, result = client.settlement(config)

        if result.approved:
            state = 'approved'
        elif result.pos_resp_code:
            state = 'declined'
        else:
            state = 'unknown'

        transaction = self.env['afs.transaction'].create({
            'operation': 'settlement',
            'terminal_id': self.id,
            'company_id': self.company_id.id,
            'state': state,
            'request_xml': afs_client.redact_secure_key(request_xml),
            'response_xml': response_xml,
            'web_response_status': result.web_response_status,
            'pos_resp_code': result.pos_resp_code,
            'pos_resp_text': result.pos_resp_text,
            'pos_resp_status': result.pos_resp_status,
            'pos_batch_number': result.pos_batch_number,
            'pos_datetime': result.pos_datetime,
        })
        # The Settlement response has no PosBatchNumber field per the AFS spec
        # (§8.9) — record the date from the settlement actually being
        # approved, not from a field AFS never sends. Capture a batch number
        # too if AFS ever does provide one, opportunistically.
        if result.approved:
            vals = {'last_settlement_date': fields.Datetime.now()}
            if result.pos_batch_number:
                vals['last_settlement_batch_number'] = result.pos_batch_number
            self.write(vals)
        return transaction
