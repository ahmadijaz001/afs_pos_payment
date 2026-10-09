# -*- coding: utf-8 -*-
"""print_pos_receipt / print_receipt_note changed from Boolean to Selection
(WSDL-defined enums, not booleans — see services/afs_client.py). Odoo's schema
upgrade only widens the column (boolean -> varchar); it does not remap the old
True/False text into a valid selection key, so any terminal created under the
old field type is left with an unusable value ("true"/"false") that crashes
int(terminal.print_pos_receipt) in the wizard. Map old -> new explicitly."""


def migrate(cr, version):
    cr.execute("""
        UPDATE afs_terminal
        SET print_pos_receipt = CASE
            WHEN print_pos_receipt IN ('true', 't', '1') THEN '3'
            ELSE '0'
        END
        WHERE print_pos_receipt NOT IN ('0', '1', '2', '3')
    """)
    cr.execute("""
        UPDATE afs_terminal
        SET print_receipt_note = CASE
            WHEN print_receipt_note IN ('true', 't', '1') THEN '1'
            ELSE '0'
        END
        WHERE print_receipt_note NOT IN ('0', '1', '2', '3', '4')
    """)
