# -*- coding: utf-8 -*-
"""18.0.4.0.0: the terminal advisory lock was replaced by afs.transaction
.finished_at. Sale/Void records that completed before this field existed
must not count as "in flight" — mark every non-pending one finished.

Also flag terminals whose secure key parameter does not follow the new
'afs_pos.secure_key.' prefix: they keep working only once an administrator
renames the system parameter and updates the terminal."""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("""
        UPDATE afs_transaction
           SET finished_at = COALESCE(write_date, create_date)
         WHERE finished_at IS NULL AND state != 'pending'
    """)
    cr.execute(r"""
        SELECT name, secure_key_param FROM afs_terminal
         WHERE secure_key_param IS NULL OR secure_key_param NOT LIKE 'afs\_pos.secure\_key.%'
    """)
    for name, param in cr.fetchall():
        _logger.warning(
            'AFS terminal %r uses secure key parameter %r, which does not start with '
            '"afs_pos.secure_key.": it will refuse to transact until an administrator '
            'moves the key to a parameter with that prefix.', name, param)
