# -*- coding: utf-8 -*-
import logging
from datetime import timedelta
from urllib.parse import urlparse

import psycopg2.errors
import pytz

from odoo import api, fields, models, _
from odoo.addons.base.models.res_partner import _tz_get
from odoo.exceptions import AccessError, UserError, ValidationError

from ..services import afs_client

_logger = logging.getLogger(__name__)

# Every terminal's MerchantSecureKey must live under this prefix. Without it,
# anyone who can edit a terminal could point secure_key_param at an unrelated
# system parameter (database.secret, another integration's API token...) and
# have it sent out as MerchantSecureKey.
SECURE_KEY_PARAM_PREFIX = 'afs_pos.secure_key.'
# Endpoint hosts accepted out of the box; administrators can allow more via
# the 'afs_pos_integration.allowed_endpoint_hosts' system parameter
# (comma-separated), which only base.group_system can edit.
DEFAULT_ALLOWED_HOST_SUFFIX = '.afs.com.bh'
ALLOWED_HOSTS_PARAM = 'afs_pos_integration.allowed_endpoint_hosts'
# Fields that decide where the secret goes and which secret is sent:
# Settings administrators only, never terminal managers.
ADMIN_ONLY_FIELDS = {'secure_key_param', 'endpoint_url'}
# Sale/Void calls that hold a terminal: anything started within this window
# past the terminal's own timeout, and not yet finished, counts as in flight.
BUSY_GRACE_SECONDS = 120


def check_afs_manager(env):
    """Server-side guard for real-money actions. A button's groups= only hides
    it; the method itself is still callable over RPC."""
    if not env.su and not env.user.has_group('afs_pos_integration.group_afs_manager'):
        raise AccessError(env._('Only AFS managers can perform this operation.'))


def check_afs_user(env):
    if not env.su and not env.user.has_group('afs_pos_integration.group_afs_user'):
        raise AccessError(env._('You are not allowed to take card payments.'))


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
             "MerchantSecureKey. Must start with 'afs_pos.secure_key.', e.g. "
             "'afs_pos.secure_key.reception_dubai'. The key itself is never "
             "stored on this record. Only Settings administrators can change it.",
    )
    endpoint_url = fields.Char(
        required=True,
        default='https://ereceipt.afs.com.bh/Ecr.Ae.Afs/EcrComInterface.svc',
        help='HTTPS only, on an AFS host (*.afs.com.bh) or a host listed in the '
             "'afs_pos_integration.allowed_endpoint_hosts' system parameter. "
             'Only Settings administrators can change it.',
    )
    integrator_name = fields.Char(default='RAALC')
    tz = fields.Selection(
        _tz_get, string='Terminal Timezone', required=True, default='Asia/Bahrain',
        help='Timezone of the date/time AFS returns (PosDate/PosTime) and prints on '
             'receipts. Verified live on the UAE endpoint: UTC+3 (AFS is Bahrain-based), '
             'not Dubai time. Used to convert it to UTC before storing or comparing it.',
    )

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

    timeout = fields.Integer(
        default=180,
        help='Seconds to wait for the terminal to answer. Note: the AFS endpoint sits behind '
             'Cloudflare, which drops any request still open after 100 seconds (HTTP 524) — '
             'the operator and cardholder must finish on the terminal well within that, '
             'whatever this value is.')
    max_amount = fields.Monetary(currency_field='currency_id', help='Per-transaction ceiling. 0 = no limit.')

    user_ids = fields.Many2many('res.users', string='Restricted To Users',
                                 help='Leave empty to allow any AFS user to use this terminal.')

    void_delay_seconds = fields.Integer(
        string='Void Delay After Sale (s)', default=120,
        help='Minimum seconds between a sale being approved and its Void. A Void '
             'sent right after the sale is carried out by the terminal, but the AFS '
             'server never returns its answer (HTTP 524 after 100 s) and keeps no '
             'record of it. Seen live: Voids sent 1-3 s after the sale were lost; '
             'one sent 4 minutes later answered in 10 s. 0 = no delay.')
    void_requires_open_batch = fields.Boolean(
        string='Void Requires Open Batch', default=True,
        help='Refuse Void once a Settlement has run on this terminal since the '
             'sale being reversed. Switch off only after confirming with AFS '
             'that Void succeeds across settled batches. Note: only settlements '
             'run from Odoo are known here — if AFS also auto-settles the '
             'terminal, this check cannot see it.',
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

    # ------------------------------------------------------------------
    # Credential / endpoint protection
    # ------------------------------------------------------------------

    def _check_admin_only_fields(self, vals):
        if self.env.su or not (ADMIN_ONLY_FIELDS & set(vals)):
            return
        if not self.env.user.has_group('base.group_system'):
            raise AccessError(_(
                'Only Settings administrators can change the secure key parameter '
                'or the endpoint URL of an AFS terminal.'
            ))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._check_admin_only_fields(vals)
        return super().create(vals_list)

    def write(self, vals):
        self._check_admin_only_fields(vals)
        return super().write(vals)

    @api.constrains('secure_key_param')
    def _check_secure_key_param(self):
        for terminal in self:
            self._validate_secure_key_param(terminal.secure_key_param)

    @api.constrains('endpoint_url')
    def _check_endpoint_url(self):
        for terminal in self:
            terminal._validate_endpoint_url(terminal.endpoint_url)

    @api.model
    def _validate_secure_key_param(self, param):
        if not param or not param.startswith(SECURE_KEY_PARAM_PREFIX) or len(param) <= len(SECURE_KEY_PARAM_PREFIX):
            raise ValidationError(_(
                'The secure key parameter must start with "%s" (e.g. "%sreception_dubai").'
            ) % (SECURE_KEY_PARAM_PREFIX, SECURE_KEY_PARAM_PREFIX))

    def _allowed_endpoint_hosts(self):
        raw = self.env['ir.config_parameter'].sudo().get_param(ALLOWED_HOSTS_PARAM, '')
        return {h.strip().lower() for h in raw.split(',') if h.strip()}

    def _validate_endpoint_url(self, url):
        parsed = urlparse(url or '')
        host = (parsed.hostname or '').lower()
        if parsed.scheme != 'https' or not host:
            raise ValidationError(_('The AFS endpoint must be an https:// URL.'))
        if not (host.endswith(DEFAULT_ALLOWED_HOST_SUFFIX) or host in self._allowed_endpoint_hosts()):
            raise ValidationError(_(
                'Host "%(host)s" is not an allowed AFS endpoint. Allowed: *%(suffix)s, or hosts '
                'an administrator lists in the "%(param)s" system parameter.',
                host=host, suffix=DEFAULT_ALLOWED_HOST_SUFFIX, param=ALLOWED_HOSTS_PARAM,
            ))

    def _get_secure_key(self):
        self.ensure_one()
        # Re-checked at use time too: records saved before the constraint
        # existed are never revalidated by Odoo on their own.
        try:
            self._validate_secure_key_param(self.secure_key_param)
        except ValidationError as exc:
            raise UserError(_('Terminal "%(name)s": %(error)s', name=self.name, error=exc.args[0]))
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
            # Spec §9.10.1: tiller username / full name are max 30 characters.
            ecr_tiller_full_name=(teller_full_name or self.teller_full_name or self.env.user.name)[:30],
            ecr_tiller_user_name=(teller_user_name or self.teller_user_name or self.env.user.login)[:30],
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
        try:
            self._validate_endpoint_url(self.endpoint_url)
        except ValidationError as exc:
            raise UserError(_('Terminal "%(name)s": %(error)s', name=self.name, error=exc.args[0]))
        return afs_client.AfsClient(endpoint_url=self.endpoint_url, timeout=self.timeout or 180)

    # ------------------------------------------------------------------
    # Response handling
    # ------------------------------------------------------------------

    def _terminal_dt_to_utc(self, dt):
        """The terminal reports its own local wall-clock time; Odoo stores UTC."""
        self.ensure_one()
        if not dt:
            return dt
        local = pytz.timezone(self.tz or 'UTC').localize(dt, is_dst=False)
        return local.astimezone(pytz.utc).replace(tzinfo=None)

    def _parse_response(self, response_xml):
        """parse_response + everything that must happen before anything is
        stored: terminal time -> UTC, and any unmasked card number masked
        (so a misbehaving terminal never makes us store a full PAN, and never
        blows up an already-charged payment on the PAN constraint)."""
        self.ensure_one()
        result = afs_client.parse_response(response_xml)
        result.pos_datetime = self._terminal_dt_to_utc(result.pos_datetime)
        if result.pos_pan and not afs_client.is_masked_pan(result.pos_pan):
            _logger.error(
                'AFS terminal %s returned a card number that is not masked. It has been '
                'masked before storage — report this to AFS (PCI).', self.tid)
            result.pos_pan = afs_client.mask_pan(result.pos_pan)
        result.pos_receipt = afs_client.scrub_pans(result.pos_receipt)
        result.response_xml = afs_client.scrub_response_xml(response_xml)
        return result

    # ------------------------------------------------------------------
    # Terminal availability
    # ------------------------------------------------------------------

    def _is_busy(self):
        """A Sale or Void is still in flight on this terminal. Replaces a
        session-level advisory lock, which cannot be released from the
        background thread's own connection: this is plain committed data, so
        every worker sees it, and a worker that dies mid-call frees the
        terminal on its own once the window passes."""
        self.ensure_one()
        since = fields.Datetime.now() - timedelta(seconds=(self.timeout or 180) + BUSY_GRACE_SECONDS)
        return bool(self.env['afs.transaction'].sudo().search_count([
            ('terminal_id', 'in', self._same_device().ids),
            ('operation', 'in', ('sale', 'void')),
            ('finished_at', '=', False),
            ('started_at', '>=', since),
        ]))

    def _same_device(self):
        """All terminal records driving the same physical device (same TID),
        e.g. one per company. AFS: "One TID should be mapped to one ECR only" —
        the device runs one request at a time, whichever record sent it."""
        self.ensure_one()
        return self.sudo().with_context(active_test=False).search([('tid', '=', self.tid)])

    def _lock_and_check_idle(self):
        """Row lock on the terminal in the CURRENT cursor, then the busy check.
        Two concurrent starts serialise on the lock: the loser either fails
        NOWAIT or, once the winner committed, sees its in-flight record."""
        self.ensure_one()
        try:
            # NO KEY UPDATE, not UPDATE: inserting an afs.transaction that
            # references this terminal takes FOR KEY SHARE on it, which plain
            # FOR UPDATE would block (settlement records its outcome on a
            # second cursor while still holding this lock).
            self.env.cr.execute(
                'SELECT id FROM afs_terminal WHERE id IN %s ORDER BY id FOR NO KEY UPDATE NOWAIT',
                (tuple(self._same_device().ids),))
        except psycopg2.errors.LockNotAvailable:
            raise UserError(_('Terminal "%s" is currently busy with another transaction.') % self.name)
        if self._is_busy():
            raise UserError(_('Terminal "%s" is currently busy with another transaction.') % self.name)

    def _check_user_allowed(self):
        self.ensure_one()
        if self.env.su:
            return
        if self.user_ids and self.env.user not in self.user_ids:
            raise AccessError(_('You are not allowed to use terminal "%s".') % self.name)
        if self.company_id not in self.env.user.company_ids:
            raise AccessError(_('Terminal "%s" belongs to a company you have no access to.') % self.name)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

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
        check_afs_manager(self.env)
        if self._is_busy():
            # RequestCancellation would cancel whatever is actually running.
            raise UserError(_('Terminal "%s" is busy with a transaction; test the connection once it is finished.') % self.name)
        client = self._get_client()
        config = self._get_config()
        try:
            _envelope, response_xml, _result = client.cancel(config)
            result = self._parse_response(response_xml)
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
        """Synchronous, but holds the terminal's row lock for the whole call so
        no Sale or Void can start on the device meanwhile. The outcome is
        always recorded — on its own committed cursor, so a transport failure
        surfaced to the user does not roll the record back with it."""
        self.ensure_one()
        check_afs_manager(self.env)
        self._lock_and_check_idle()
        client = self._get_client()
        config = self._get_config()
        request_xml = afs_client.build_settlement_envelope(config)
        try:
            response_xml = client.post('Settlement', request_xml)
        except Exception as exc:  # noqa: BLE001
            self._record_settlement({
                'state': 'unknown',
                'request_xml': request_xml,
                'error_message': _(
                    'Settlement call failed or the connection dropped (%s). Outcome unknown — '
                    'check the terminal / AFS portal before running it again.'
                ) % exc,
            })
            raise UserError(_('Settlement failed: %s. The attempt has been recorded as Unknown.') % exc)

        result = self._parse_response(response_xml)
        state = afs_client.classify(result)
        web_ok = result.web_response_status.strip().lower().startswith('success')
        if state == 'unknown' and web_ok and not result.pos_resp_code:
            # Spec §4: the documented Settlement response is just
            # WebResponseStatus=Success — that IS the approval.
            state = 'approved'

        transaction = self._record_settlement({
            'state': state,
            'request_xml': request_xml,
            'response_xml': result.response_xml,
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
        if state == 'approved':
            vals = {'last_settlement_date': fields.Datetime.now()}
            if result.pos_batch_number:
                vals['last_settlement_batch_number'] = result.pos_batch_number
            self.sudo().write(vals)
        return transaction

    def _record_settlement(self, vals):
        self.ensure_one()
        self.env.flush_all()
        with self.env.registry.cursor() as cr:
            env = self.env(cr=cr, su=True)
            txn = env['afs.transaction'].create(dict(
                vals,
                operation='settlement',
                terminal_id=self.id,
                company_id=self.company_id.id,
            ))
            txn_id = txn.id
        return self.env['afs.transaction'].browse(txn_id)
