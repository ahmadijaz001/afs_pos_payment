# -*- coding: utf-8 -*-
{
    'name': 'AFS POS Integration',
    'version': '18.0.3.0.0',
    'summary': 'Take AFS card-terminal payments directly from posted customer invoices',
    'description': '''
AFS POS Integration
====================
Lets a user take a card payment on an AFS payment terminal directly from a
posted customer invoice, records the full terminal response against that
invoice, registers and reconciles the payment, and can reverse it with Void
while the batch is still open.

Key points
----------
- Multiple terminals supported (afs.terminal), each with its own MID/TID/
  journal; secrets are kept in System Parameters, never on the terminal record.
- Every API attempt is recorded on afs.transaction (append-only audit trail),
  not just the final outcome.
- The full terminal response (auth code, RRN, STAN, batch, masked PAN, scheme,
  EMV data...) is stored on the invoice itself.
- The Sale call runs in a background thread rather than blocking the request,
  so the invoice stays interactive (with a real Cancel) while waiting on the
  cardholder; the outcome is pushed to the browser over the bus.
- Void-based reversal for full-amount refunds, with pre-flight checks for
  amount match and open-batch status; reversal goes through a credit note,
  which triggers Void automatically once posted.
- A dropped connection during Sale is never retried automatically; recovery
  is keyed on our own reference via EnquiryByRef, otherwise the transaction is
  flagged unknown for manual resolution.
    ''',
    'author': 'Ahmad Ijaz — RAALC',
    'category': 'Accounting/Accounting',
    'depends': ['account', 'base', 'bus', 'mail'],
    'external_dependencies': {
        'python': ['requests'],
    },
    'data': [
        'security/afs_security.xml',
        'security/ir.model.access.csv',
        'views/afs_terminal_views.xml',
        'views/afs_transaction_views.xml',
        'views/account_move_views.xml',
        'views/res_config_settings_views.xml',
        'views/afs_menus.xml',
        'wizard/afs_payment_wizard_views.xml',
        'data/ir_cron_data.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'afs_pos_integration/static/src/js/afs_pending_banner.js',
            'afs_pos_integration/static/src/xml/afs_pending_banner.xml',
        ],
    },
    'installable': True,
    'auto_install': False,
    'application': False,
    'license': 'LGPL-3',
}
