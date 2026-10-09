# -*- coding: utf-8 -*-
"""18.0.4.1.0: a user-cancelled card payment is now ONE record in state
'cancelled' (see afs.transaction.action_request_cancellation). Clean up the
attempts recorded the old way, where one cancel produced three rows:

  sale   'error'   "Cancelled by user request."      (the attempt)
  cancel 'unknown' WebResponseStatus Success          (the RequestCancellation call)
  sale   'unknown' "Faild" / "Cancelled By ECR"       (AFS's answer, as a "late" row)

AFS's "Cancelled By ECR" answer is a definitive "card not charged", so the
attempt and its late answer both become 'cancelled', and the successful
RequestCancellation call becomes 'approved'. Rows are kept (audit trail);
only their state changes, then the invoices' card status is recomputed."""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    # The late answer rows: AFS confirmed the cancellation, nothing was charged.
    cr.execute("""
        UPDATE afs_transaction
           SET state = 'cancelled'
         WHERE operation IN ('sale', 'void')
           AND state = 'unknown'
           AND reverses_id IS NOT NULL
           AND COALESCE(pos_resp_code, '') = ''
           AND web_response_error ILIKE '%%cancel%%'
     RETURNING reverses_id, move_id
    """)
    rows = cr.fetchall()
    original_ids = tuple({r[0] for r in rows}) or (0,)
    move_ids = {r[1] for r in rows if r[1]}

    # Their originals, marked 'error' when Cancel was pressed.
    cr.execute("""
        UPDATE afs_transaction
           SET state = 'cancelled'
         WHERE id IN %s AND state = 'error' AND error_message = 'Cancelled by user request.'
    """, (original_ids,))

    # The RequestCancellation calls themselves: AFS answered Success.
    cr.execute("""
        UPDATE afs_transaction
           SET state = 'approved'
         WHERE operation = 'cancel' AND state = 'unknown' AND web_response_status ILIKE 'success%%'
    """)

    env = api.Environment(cr, SUPERUSER_ID, {})
    if move_ids:
        moves = env['account.move'].browse(sorted(move_ids))
        env.add_to_compute(moves._fields['afs_state'], moves)
        moves.flush_recordset()

    # Voids that AFS approved but whose bookkeeping was lost (the background
    # thread's write failed with a serialization error, fixed in this
    # version): mark the sale reversed, cancel the card payment, reconcile
    # the credit note with the invoice.
    voids = env['afs.transaction'].search([('operation', '=', 'void'), ('state', '=', 'approved')])
    for void in voids.filtered('reversal_incomplete'):
        _logger.info('AFS: completing reversal for approved Void #%s', void.id)
        void._complete_reversal()
