# -*- coding: utf-8 -*-
"""AFS ECR SOAP client. Pure Python, no Odoo imports — testable standalone."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape as xml_escape

import requests

SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
TEM_NS = "http://tempuri.org/"
DC_NS = "http://schemas.datacontract.org/2004/07/"
SOAP_ACTION_BASE = "http://tempuri.org/IEcrComInterface/"

# Shared between the Sale wizard and Void (models/afs_transaction.py): an
# offset added to a terminal's id before taking a PostgreSQL advisory lock.
# Both use the SAME key per terminal, because the physical terminal can only
# run one operation at a time regardless of which Odoo flow started it.
TERMINAL_ADVISORY_LOCK_OFFSET = 913000000000

# ISO 4217 numeric -> alpha, limited to the currencies AFS quotes in the UAE/GCC.
CURRENCY_NUMERIC_TO_ALPHA = {
    "784": "AED",
    "048": "BHD",
    "512": "OMR",
    "634": "QAR",
    "682": "SAR",
    "414": "KWD",
    "840": "USD",
    "978": "EUR",
    "826": "GBP",
    "356": "INR",
}

# Masked PAN as returned by the terminal, e.g. "408578******5322"; a full PAN reaching this is a PCI incident.
PAN_MASK_RE = re.compile(r"^\d{6}\*+\d{4}$")

_AMOUNT_QUANT = Decimal("0.01")


class AfsError(Exception):
    """Malformed AFS request/response. Transport errors are left as the underlying requests exceptions."""


def is_masked_pan(pan: str) -> bool:
    return bool(pan) and bool(PAN_MASK_RE.match(pan))


@dataclass
class AfsConfig:
    """Maps 1:1 to the WSDL ``Config`` DataContract. Field order in
    ``to_xml`` is alphabetical and MUST NOT change."""

    ecr_currency_code: str
    ecr_tiller_full_name: str
    ecr_tiller_user_name: str
    integrator_name: str
    merchant_secure_key: str
    mid: str
    tenant: str
    tid: str

    def to_xml(self) -> str:
        return (
            "<dc:Config>"
            f"<dc:EcrCurrencyCode>{xml_escape(str(self.ecr_currency_code))}</dc:EcrCurrencyCode>"
            f"<dc:EcrTillerFullName>{xml_escape(str(self.ecr_tiller_full_name))}</dc:EcrTillerFullName>"
            f"<dc:EcrTillerUserName>{xml_escape(str(self.ecr_tiller_user_name))}</dc:EcrTillerUserName>"
            f"<dc:IntegratorName>{xml_escape(str(self.integrator_name))}</dc:IntegratorName>"
            f"<dc:MerchantSecureKey>{xml_escape(str(self.merchant_secure_key))}</dc:MerchantSecureKey>"
            f"<dc:Mid>{xml_escape(str(self.mid))}</dc:Mid>"
            f"<dc:Tenant>{xml_escape(str(self.tenant))}</dc:Tenant>"
            f"<dc:Tid>{xml_escape(str(self.tid))}</dc:Tid>"
            "</dc:Config>"
        )


@dataclass
class AfsPrinter:
    """Maps 1:1 to the WSDL ``Printer`` DataContract. Field order in ``to_xml`` is alphabetical and MUST NOT change."""

    # Short generated token, never the Odoo invoice number: the terminal
    # overwrites InvoiceNumber with its own batch sequence anyway, and
    # whatever is sent here can print on the cardholder's receipt.
    invoice_number: str
    reference_number: str
    # NOT booleans — WSDL-defined enums (spec §8.2):
    # EnablePrintPosReceipt: 0=None, 1=Merchant Copy, 2=Customer Copy, 3=Both Copies.
    # EnablePrintReceiptNote: 0=None, 1=Before Header, 2=After Header, 3=Before Footer, 4=After Footer.
    enable_print_pos_receipt: int = 3
    enable_print_receipt_note: int = 0
    printer_width: int = 32
    receipt_note: str = ""

    def to_xml(self) -> str:
        return (
            "<dc:Printer>"
            f"<dc:EnablePrintPosReceipt>{int(self.enable_print_pos_receipt)}</dc:EnablePrintPosReceipt>"
            f"<dc:EnablePrintReceiptNote>{int(self.enable_print_receipt_note)}</dc:EnablePrintReceiptNote>"
            f"<dc:InvoiceNumber>{xml_escape(str(self.invoice_number))}</dc:InvoiceNumber>"
            f"<dc:PrinterWidth>{int(self.printer_width)}</dc:PrinterWidth>"
            f"<dc:ReceiptNote>{xml_escape(str(self.receipt_note))}</dc:ReceiptNote>"
            f"<dc:ReferenceNumber>{xml_escape(str(self.reference_number))}</dc:ReferenceNumber>"
            "</dc:Printer>"
        )


@dataclass
class AfsResult:
    approved: bool
    web_response_status: str = ""
    web_response_error: str = ""
    pos_resp_code: str = ""
    pos_resp_text: str = ""
    pos_resp_status: str = ""
    pos_amount: Decimal | None = None
    pos_currency_code: str = ""
    pos_currency_alpha: str = ""
    pos_auth_code: str = ""
    pos_rrn: str = ""
    pos_stan: str = ""
    pos_invoice_number: str = ""
    pos_batch_number: str = ""
    pos_pan: str = ""
    pos_issuer_name: str = ""
    pos_scheme: str = ""
    pos_emv_aid: str = ""
    pos_emv_tvr: str = ""
    pos_emv_cryptogram: str = ""
    pos_entry_mode: str = ""
    pos_cvm_id: str = ""
    pos_datetime: datetime | None = None
    pos_txn_name: str = ""
    pos_receipt: str = ""
    raw_fields: dict = field(default_factory=dict)
    response_xml: str = ""


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _collect_leaf_fields(xml_text: str) -> dict:
    """Flatten every leaf element into {local_tag: text}, ignoring namespace prefixes."""
    root = ET.fromstring(xml_text)
    fields = {}
    for el in root.iter():
        if len(el) == 0 and el.text is not None:
            fields[_local_name(el.tag)] = el.text.strip()
    return fields


def _parse_amount(raw: str | None) -> Decimal | None:
    # PosAmount has ten decimal places and is already in major units (e.g. "1.0000000000" == AED 1.00).
    if not raw:
        return None
    try:
        return Decimal(raw).quantize(_AMOUNT_QUANT, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None


def _parse_datetime(pos_date: str | None, pos_time: str | None) -> datetime | None:
    if not pos_date or not pos_time:
        return None
    try:
        return datetime.strptime(pos_date + pos_time, "%Y%m%d%H%M%S")
    except ValueError:
        return None


def _currency_alpha(numeric: str | None) -> str:
    # PosCurrencyCode is the ISO 4217 *numeric* code (e.g. "784" == AED), not a letter code.
    if not numeric:
        return ""
    return CURRENCY_NUMERIC_TO_ALPHA.get(numeric.strip().zfill(3), "")


def parse_response(xml_text: str) -> AfsResult:
    fields = _collect_leaf_fields(xml_text)

    web_status = fields.get("WebResponseStatus", "")
    resp_code = fields.get("PosRespCode", "")
    resp_status = fields.get("PosRespStatus", "")
    # Two independent signals for approval: the ISO-style PosRespCode ("00"/"000")
    # and PosRespStatus (spec: -1 unknown / 0 declined / 1 approved). Accept either,
    # gated by the web call itself having succeeded — some responses populate one
    # but not the other.
    approved = web_status.strip().lower().startswith("success") and (
        resp_code.strip() in ("00", "000") or resp_status.strip() == "1"
    )

    return AfsResult(
        approved=approved,
        web_response_status=web_status,
        web_response_error=fields.get("WebResponseErrorDesc", ""),
        pos_resp_code=resp_code,
        pos_resp_text=fields.get("PosRespText", ""),
        pos_resp_status=resp_status,
        pos_amount=_parse_amount(fields.get("PosAmount")),
        pos_currency_code=fields.get("PosCurrencyCode", ""),
        pos_currency_alpha=_currency_alpha(fields.get("PosCurrencyCode")),
        pos_auth_code=fields.get("PosAuthCode", ""),
        pos_rrn=fields.get("PosRRN", ""),
        pos_stan=fields.get("PosStan", ""),
        pos_invoice_number=fields.get("PosInvoiceNumber", ""),
        pos_batch_number=fields.get("PosBatchNumber", ""),
        pos_pan=fields.get("PosPan", ""),
        pos_issuer_name=fields.get("PosIssuerName", ""),
        pos_scheme=fields.get("PosEmvAppName", ""),
        pos_emv_aid=fields.get("PosEmvAid", ""),
        pos_emv_tvr=fields.get("PosEmvTVR", ""),
        pos_emv_cryptogram=fields.get("PosEmvCryptogram", ""),
        pos_entry_mode=fields.get("PosCardEntryModeId", ""),
        pos_cvm_id=fields.get("PosCVMId", ""),
        pos_datetime=_parse_datetime(fields.get("PosDate"), fields.get("PosTime")),
        pos_txn_name=fields.get("PosTxnName", ""),
        pos_receipt=fields.get("PosReceipt", ""),
        raw_fields=fields,
        response_xml=xml_text,
    )


_SECURE_KEY_RE = re.compile(
    r"(<(?:[\w]+:)?MerchantSecureKey[^>]*>)([^<]*)(</(?:[\w]+:)?MerchantSecureKey>)"
)


def redact_secure_key(xml_text: str) -> str:
    """Replace MerchantSecureKey's text with '***' — call before storing/logging any request XML."""
    return _SECURE_KEY_RE.sub(lambda m: m.group(1) + "***" + m.group(3), xml_text)


def _envelope(operation: str, body_xml: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<soapenv:Envelope xmlns:soapenv="{SOAP_NS}" xmlns:tem="{TEM_NS}" xmlns:dc="{DC_NS}">'
        "<soapenv:Header/>"
        "<soapenv:Body>"
        f"<tem:{operation}><tem:webReq>{body_xml}</tem:webReq></tem:{operation}>"
        "</soapenv:Body>"
        "</soapenv:Envelope>"
    )


def build_sale_envelope(config: AfsConfig, amount: Decimal, printer: AfsPrinter) -> str:
    amount_str = format(Decimal(amount).quantize(_AMOUNT_QUANT, rounding=ROUND_HALF_UP), "f")
    body = config.to_xml() + f"<dc:EcrAmount>{amount_str}</dc:EcrAmount>" + printer.to_xml()
    return _envelope("Sale", body)


def _orig_ref_body(config: AfsConfig, orig_auth_code: str, orig_invoice_number: str, orig_rrn: str, printer: AfsPrinter) -> str:
    return (
        config.to_xml()
        + f"<dc:OrigAuthCode>{xml_escape(str(orig_auth_code))}</dc:OrigAuthCode>"
        + f"<dc:OrigInvoiceNumber>{xml_escape(str(orig_invoice_number))}</dc:OrigInvoiceNumber>"
        + f"<dc:OrigRrn>{xml_escape(str(orig_rrn))}</dc:OrigRrn>"
        + printer.to_xml()
    )


def build_void_envelope(config: AfsConfig, orig_auth_code: str, orig_invoice_number: str, orig_rrn: str, printer: AfsPrinter) -> str:
    return _envelope("Void", _orig_ref_body(config, orig_auth_code, orig_invoice_number, orig_rrn, printer))


def build_enquiry_envelope(config: AfsConfig, orig_auth_code: str = "", orig_invoice_number: str = "", orig_rrn: str = "", printer: AfsPrinter | None = None) -> str:
    printer = printer or AfsPrinter(invoice_number="", reference_number="")
    return _envelope("Enquiry", _orig_ref_body(config, orig_auth_code, orig_invoice_number, orig_rrn, printer))


def build_enquiry_by_ref_envelope(config: AfsConfig, orig_auth_code: str, orig_invoice_number: str, orig_rrn: str, printer: AfsPrinter) -> str:
    return _envelope("EnquiryByRef", _orig_ref_body(config, orig_auth_code, orig_invoice_number, orig_rrn, printer))


def build_settlement_envelope(config: AfsConfig) -> str:
    return _envelope("Settlement", config.to_xml())


def build_cancellation_envelope(config: AfsConfig) -> str:
    return _envelope("RequestCancellation", config.to_xml())


def correlate_recovery(result: AfsResult, expected_amount: Decimal, sale_started_at: datetime, skew_seconds: int = 60) -> bool:
    """After a dropped Sale connection: trust a no-reference Enquiry only if amount AND timestamp both match; else unknown."""
    if result.pos_amount is None or result.pos_datetime is None:
        return False
    if result.pos_amount != Decimal(expected_amount).quantize(_AMOUNT_QUANT, rounding=ROUND_HALF_UP):
        return False
    return result.pos_datetime >= (sale_started_at - timedelta(seconds=skew_seconds))


class AfsClient:
    """Thin transport wrapper — the only part of this module that talks to ``requests``."""

    def __init__(self, endpoint_url: str, timeout: int = 180):
        self.endpoint_url = endpoint_url
        self.timeout = timeout

    def post(self, operation: str, envelope: str) -> str:
        # Public (not the sale/void/... helpers below) so a caller can build the envelope,
        # commit it to the DB, and only then make the (up to 180s) blocking call.
        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": f"{SOAP_ACTION_BASE}{operation}",
        }
        resp = requests.post(self.endpoint_url, data=envelope.encode("utf-8"), headers=headers, timeout=self.timeout)
        resp.raise_for_status()
        return resp.text

    def sale(self, config: AfsConfig, amount: Decimal, printer: AfsPrinter):
        envelope = build_sale_envelope(config, amount, printer)
        response_xml = self.post("Sale", envelope)
        return envelope, response_xml, parse_response(response_xml)

    def void(self, config: AfsConfig, orig_auth_code: str, orig_invoice_number: str, orig_rrn: str, printer: AfsPrinter):
        envelope = build_void_envelope(config, orig_auth_code, orig_invoice_number, orig_rrn, printer)
        response_xml = self.post("Void", envelope)
        return envelope, response_xml, parse_response(response_xml)

    def enquiry(self, config: AfsConfig, printer: AfsPrinter | None = None):
        envelope = build_enquiry_envelope(config, printer=printer)
        response_xml = self.post("Enquiry", envelope)
        return envelope, response_xml, parse_response(response_xml)

    def enquiry_by_ref(self, config: AfsConfig, orig_auth_code: str, orig_invoice_number: str, orig_rrn: str, printer: AfsPrinter):
        envelope = build_enquiry_by_ref_envelope(config, orig_auth_code, orig_invoice_number, orig_rrn, printer)
        response_xml = self.post("EnquiryByRef", envelope)
        return envelope, response_xml, parse_response(response_xml)

    def settlement(self, config: AfsConfig):
        envelope = build_settlement_envelope(config)
        response_xml = self.post("Settlement", envelope)
        return envelope, response_xml, parse_response(response_xml)

    def cancel(self, config: AfsConfig):
        envelope = build_cancellation_envelope(config)
        response_xml = self.post("RequestCancellation", envelope)
        return envelope, response_xml, parse_response(response_xml)
