# -*- coding: utf-8 -*-
"""Pure-Python tests for services/afs_client.py — no Odoo, no database.

Run standalone with:
    cd custom/afs_pos_integration/tests && python -m unittest test_client -v
(run from this directory, as a top-level module, so importing it never
triggers afs_pos_integration/__init__.py and its Odoo-only dependencies —
see the sys.path handling below.)
"""
import os
import sys
import unittest
from datetime import datetime
from decimal import Decimal
from xml.etree import ElementTree as ET

# Import services.afs_client directly by path, NOT via the package's relative
# import (`from ..services import afs_client`). Going through the package
# would execute afs_pos_integration/__init__.py, which imports models/ and
# therefore requires a working `odoo` installation — defeating the point of
# this file being runnable standalone with no Odoo, no database.
_MODULE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _MODULE_ROOT not in sys.path:
    sys.path.insert(0, _MODULE_ROOT)

from services import afs_client  # noqa: E402

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), 'fixtures')


def _read_fixture(name):
    with open(os.path.join(FIXTURES_DIR, name), encoding='utf-8') as f:
        return f.read()


def _make_config():
    return afs_client.AfsConfig(
        ecr_currency_code='784',
        ecr_tiller_full_name='Reception',
        ecr_tiller_user_name='reception',
        integrator_name='RAALC',
        merchant_secure_key='TOP-SECRET-KEY',
        mid='400000436830001',
        tenant='raalc-tenant',
        tid='40046947',
    )


def _make_printer(token='A7F3C119'):
    return afs_client.AfsPrinter(invoice_number=token, reference_number=token)


class TestApprovalParsing(unittest.TestCase):

    def test_approved_fixture(self):
        xml_text = _read_fixture('sale_approved.xml')
        result = afs_client.parse_response(xml_text)
        self.assertTrue(result.approved)
        self.assertEqual(result.pos_resp_status, '1')
        self.assertEqual(result.pos_amount, Decimal('1.00'))
        self.assertEqual(result.pos_currency_alpha, 'AED')

    def test_decline_even_with_successful_web_call(self):
        xml_text = _read_fixture('sale_approved.xml').replace(
            '<a:PosRespCode>00</a:PosRespCode>', '<a:PosRespCode>05</a:PosRespCode>'
        ).replace(
            '<a:PosRespStatus>1</a:PosRespStatus>', '<a:PosRespStatus>0</a:PosRespStatus>'
        )
        result = afs_client.parse_response(xml_text)
        self.assertEqual(result.web_response_status, 'Success')
        self.assertFalse(result.approved)

    def test_approved_via_resp_status_alone(self):
        # PosRespCode does not always populate "00" — PosRespStatus=1 alone must also approve.
        xml_text = _read_fixture('sale_approved.xml').replace(
            '<a:PosRespCode>00</a:PosRespCode>', '<a:PosRespCode>0</a:PosRespCode>'
        )
        result = afs_client.parse_response(xml_text)
        self.assertTrue(result.approved)

    def test_web_response_error_desc_captured(self):
        xml_text = _read_fixture('sale_approved.xml').replace(
            '<a:WebResponseStatus>Success</a:WebResponseStatus>',
            '<a:WebResponseStatus>Faild</a:WebResponseStatus>'
            '<a:WebResponseErrorDesc>Terminal offline</a:WebResponseErrorDesc>'
        )
        result = afs_client.parse_response(xml_text)
        self.assertFalse(result.approved)
        self.assertEqual(result.web_response_error, 'Terminal offline')


class TestAmountAndCurrencyFormatting(unittest.TestCase):

    def test_amount_ten_decimals_to_two(self):
        self.assertEqual(afs_client._parse_amount('1.0000000000'), Decimal('1.00'))
        self.assertEqual(afs_client._parse_amount('1500.0000000000'), Decimal('1500.00'))

    def test_currency_numeric_to_alpha(self):
        self.assertEqual(afs_client._currency_alpha('784'), 'AED')
        self.assertEqual(afs_client._currency_alpha('048'), 'BHD')
        self.assertEqual(afs_client._currency_alpha('999'), '')


class TestElementOrdering(unittest.TestCase):
    """WCF DataContract deserialisation is order-sensitive; an out-of-sequence
    element is silently dropped. Lock the alphabetical order in place."""

    def _child_tags(self, xml_text, parent_local_name):
        root = ET.fromstring(xml_text)
        for el in root.iter():
            if el.tag.rsplit('}', 1)[-1] == parent_local_name:
                return [child.tag.rsplit('}', 1)[-1] for child in el]
        raise AssertionError('%s not found in envelope' % parent_local_name)

    def test_config_order(self):
        envelope = afs_client.build_settlement_envelope(_make_config())
        self.assertEqual(self._child_tags(envelope, 'Config'), [
            'EcrCurrencyCode', 'EcrTillerFullName', 'EcrTillerUserName',
            'IntegratorName', 'MerchantSecureKey', 'Mid', 'Tenant', 'Tid',
        ])

    def test_printer_order(self):
        envelope = afs_client.build_sale_envelope(_make_config(), Decimal('1500.00'), _make_printer())
        self.assertEqual(self._child_tags(envelope, 'Printer'), [
            'EnablePrintPosReceipt', 'EnablePrintReceiptNote', 'InvoiceNumber',
            'PrinterWidth', 'ReceiptNote', 'ReferenceNumber',
        ])

    def test_sale_body_order(self):
        envelope = afs_client.build_sale_envelope(_make_config(), Decimal('1500.00'), _make_printer())
        self.assertEqual(self._child_tags(envelope, 'webReq'), ['Config', 'EcrAmount', 'Printer'])

    def test_void_body_order(self):
        envelope = afs_client.build_void_envelope(_make_config(), 'AUTH1', 'INV1', 'RRN1', _make_printer())
        self.assertEqual(self._child_tags(envelope, 'webReq'), [
            'Config', 'OrigAuthCode', 'OrigInvoiceNumber', 'OrigRrn', 'Printer',
        ])

    def test_settlement_body_order(self):
        envelope = afs_client.build_settlement_envelope(_make_config())
        self.assertEqual(self._child_tags(envelope, 'webReq'), ['Config'])


class TestSecureKeyRedaction(unittest.TestCase):

    def test_key_never_appears_after_redaction(self):
        envelope = afs_client.build_sale_envelope(_make_config(), Decimal('1500.00'), _make_printer())
        self.assertIn('TOP-SECRET-KEY', envelope)
        redacted = afs_client.redact_secure_key(envelope)
        self.assertNotIn('TOP-SECRET-KEY', redacted)
        self.assertIn('<dc:MerchantSecureKey>***</dc:MerchantSecureKey>', redacted)

    def test_redaction_preserves_everything_else(self):
        envelope = afs_client.build_sale_envelope(_make_config(), Decimal('1500.00'), _make_printer())
        redacted = afs_client.redact_secure_key(envelope)
        self.assertIn('<dc:Mid>400000436830001</dc:Mid>', redacted)
        self.assertIn('<dc:EcrAmount>1500.00</dc:EcrAmount>', redacted)


class TestPanMasking(unittest.TestCase):

    def test_masked_pan_accepted(self):
        self.assertTrue(afs_client.is_masked_pan('408578******5322'))

    def test_full_pan_rejected(self):
        self.assertFalse(afs_client.is_masked_pan('4085781234565322'))
        self.assertFalse(afs_client.is_masked_pan(''))

    def test_other_masking_styles_accepted(self):
        self.assertTrue(afs_client.is_masked_pan('************5322'))
        self.assertTrue(afs_client.is_masked_pan('4085XXXXXXXX5322'))
        self.assertTrue(afs_client.is_masked_pan('40857812****5322'))

    def test_too_many_visible_digits_rejected(self):
        # 9 leading digits visible is more than PCI DSS allows.
        self.assertFalse(afs_client.is_masked_pan('408578123***5322'))

    def test_mask_pan_keeps_last_four_only(self):
        masked = afs_client.mask_pan('4085781234565322')
        self.assertTrue(masked.endswith('5322'))
        self.assertNotIn('408578', masked)
        self.assertTrue(afs_client.is_masked_pan(masked))

    def test_scrub_pans_masks_luhn_valid_numbers_only(self):
        text = 'CARD 4111111111111111 RRN 625969470006 REF 1234567890123'
        scrubbed = afs_client.scrub_pans(text)
        self.assertNotIn('4111111111111111', scrubbed)
        self.assertIn('1111', scrubbed)
        self.assertIn('625969470006', scrubbed)  # 12 digits: not a PAN candidate
        self.assertIn('1234567890123', scrubbed)  # fails Luhn: left alone

    def test_scrub_response_xml_masks_unmasked_pan_element(self):
        xml = '<a:PosPan>4111111111111111</a:PosPan><a:PosRRN>625969470006</a:PosRRN>'
        scrubbed = afs_client.scrub_response_xml(xml)
        self.assertNotIn('4111111111111111', scrubbed)
        self.assertIn('<a:PosRRN>625969470006</a:PosRRN>', scrubbed)

    def test_scrub_response_xml_keeps_masked_pan(self):
        xml = '<a:PosPan>408578******5322</a:PosPan>'
        self.assertEqual(afs_client.scrub_response_xml(xml), xml)


class TestCancelledResponse(unittest.TestCase):

    def test_cancelled_by_ecr_is_cancelled_not_unknown(self):
        xml = ('<r xmlns:a="x"><a:PosAmount>0.0000000000</a:PosAmount><a:PosRespStatus>0</a:PosRespStatus>'
               '<a:WebResponseErrorDesc>Cancelled By ECR</a:WebResponseErrorDesc>'
               '<a:WebResponseStatus>Faild</a:WebResponseStatus></r>')
        result = afs_client.parse_response(xml)
        self.assertFalse(result.approved)
        self.assertTrue(result.cancelled)

    def test_decline_is_not_a_cancellation(self):
        xml = ('<r><PosRespCode>05</PosRespCode><PosRespText>Declined</PosRespText>'
               '<WebResponseStatus>Success</WebResponseStatus></r>')
        self.assertFalse(afs_client.parse_response(xml).cancelled)


class TestClassify(unittest.TestCase):
    """Outcome rules from spec §10 and the live UAE endpoint."""

    def _r(self, **kw):
        return afs_client.AfsResult(approved=kw.pop('approved', False), **kw)

    def test_approved(self):
        self.assertEqual(afs_client.classify(self._r(approved=True)), 'approved')

    def test_declined_needs_status_0_and_a_response_code(self):
        r = self._r(web_response_status='Success', pos_resp_status='0', pos_resp_code='05')
        self.assertEqual(afs_client.classify(r), 'declined')

    def test_status_minus_one_is_unknown_not_declined(self):
        r = self._r(web_response_status='Success', pos_resp_status='-1', pos_resp_code='91')
        self.assertEqual(afs_client.classify(r), 'unknown')

    def test_no_record_is_unknown_not_declined(self):
        # Live EnquiryByRef for a reference AFS never saw: Success, status 0, nothing else.
        r = self._r(web_response_status='Success', pos_resp_status='0')
        self.assertEqual(afs_client.classify(r), 'unknown')

    def test_web_failed_is_unknown(self):
        r = self._r(web_response_status='Faild', web_response_error='Terminal not connected')
        self.assertEqual(afs_client.classify(r), 'unknown')

    def test_spec_void_sample_with_txn_name_sale_is_approved(self):
        xml = ('<r xmlns:a="x"><a:PosAuthCode>656324</a:PosAuthCode><a:PosRRN>102171037110</a:PosRRN>'
               '<a:PosRespCode>00</a:PosRespCode><a:PosRespStatus>1</a:PosRespStatus>'
               '<a:PosTxnName>Sale</a:PosTxnName><a:WebResponseStatus>Success</a:WebResponseStatus></r>')
        self.assertEqual(afs_client.classify(afs_client.parse_response(xml)), 'approved')


class TestXmlSafety(unittest.TestCase):

    def test_dtd_refused(self):
        xml = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]>'
               '<r><WebResponseStatus>&lol;</WebResponseStatus></r>')
        with self.assertRaises(afs_client.AfsError):
            afs_client.parse_response(xml)


class TestRecoveryCorrelation(unittest.TestCase):

    def _result(self, amount, dt):
        return afs_client.AfsResult(approved=True, pos_amount=Decimal(amount), pos_datetime=dt)

    def test_matching_amount_and_recent_timestamp_correlates(self):
        started_at = datetime(2026, 9, 16, 11, 0, 0)
        result = self._result('1500.00', datetime(2026, 9, 16, 11, 0, 30))
        self.assertTrue(afs_client.correlate_recovery(result, Decimal('1500.00'), started_at))

    def test_amount_mismatch_does_not_correlate(self):
        started_at = datetime(2026, 9, 16, 11, 0, 0)
        result = self._result('999.00', datetime(2026, 9, 16, 11, 0, 30))
        self.assertFalse(afs_client.correlate_recovery(result, Decimal('1500.00'), started_at))

    def test_stale_timestamp_does_not_correlate(self):
        started_at = datetime(2026, 9, 16, 11, 0, 0)
        result = self._result('1500.00', datetime(2026, 9, 16, 10, 0, 0))
        self.assertFalse(afs_client.correlate_recovery(result, Decimal('1500.00'), started_at))

    def test_missing_fields_do_not_correlate(self):
        started_at = datetime(2026, 9, 16, 11, 0, 0)
        result = afs_client.AfsResult(approved=True)
        self.assertFalse(afs_client.correlate_recovery(result, Decimal('1500.00'), started_at))


if __name__ == '__main__':
    unittest.main()
