# -*- coding: utf-8 -*-
"""AFS ECR SOAP client. Pure Python, no Odoo imports — testable standalone."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape as xml_escape

import requests

SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
TEM_NS = "http://tempuri.org/"
DC_NS = "http://schemas.datacontract.org/2004/07/"
SOAP_ACTION_BASE = "http://tempuri.org/IEcrComInterface/"

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

# Masked PAN as returned by the terminal, e.g. "408578******5322", "************5322"
# or "4085XXXXXXXX5322": at most the first 8 and last 4 digits visible (PCI DSS),
# 12-19 characters overall. A full PAN reaching this is a PCI incident.
PAN_MASK_RE = re.compile(r"^(?=.{12,19}$)\d{0,8}[*Xx#]{3,}\d{0,4}$")
# Candidate full PANs inside free text (receipts): 13-19 consecutive digits.
_PAN_CANDIDATE_RE = re.compile(r"(?<!\d)\d{13,19}(?!\d)")
# Raw-XML elements that can carry card numbers.
_PAN_ELEMENT_RE = re.compile(
    r"(<(?:[\w]+:)?(?:PosPan|[\w]*Track[\w]*)[^>]*>)([^<]*)(</(?:[\w]+:)?(?:PosPan|[\w]*Track[\w]*)>)"
)

_AMOUNT_QUANT = Decimal("0.01")


class AfsError(Exception):
    """Malformed AFS request/response. Transport errors are left as the underlying requests exceptions."""


def is_masked_pan(pan: str) -> bool:
    return bool(pan) and bool(PAN_MASK_RE.match(pan))


def mask_pan(pan: str) -> str:
    """Keep only the last 4 digits. Used when a terminal hands back something
    that is not a properly masked PAN — never store it as received."""
    digits = re.sub(r"\D", "", pan or "")
    if len(digits) < 4:
        return "*" * 12
    return "*" * max(len(digits) - 4, 8) + digits[-4:]


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def scrub_pans(text: str) -> str:
    """Mask anything in free text that looks like a full card number (13-19
    digits passing the Luhn check)."""
    if not text:
        return text
    return _PAN_CANDIDATE_RE.sub(lambda m: mask_pan(m.group(0)) if _luhn_ok(m.group(0)) else m.group(0), text)


def scrub_response_xml(xml_text: str) -> str:
    """Mask PAN/track elements that are not already masked — call before storing raw response XML."""
    if not xml_text:
        return xml_text

    def _sub(m):
        value = m.group(2).strip()
        if not value or is_masked_pan(value):
            return m.group(0)
        return m.group(1) + mask_pan(value) + m.group(3)

    return _PAN_ELEMENT_RE.sub(_sub, xml_text)


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
    # The request was stopped before anything was charged, e.g.
    # WebResponseStatus "Faild" + WebResponseErrorDesc "Cancelled By ECR" in
    # answer to our own RequestCancellation. A definitive "not charged".
    cancelled: bool = False
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
    response_xml: str = ""


def is_web_success(web_response_status: str) -> bool:
    """WebResponseStatus says the web call itself succeeded ("Success"; failures are "Faild")."""
    return (web_response_status or "").strip().lower().startswith("success")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _collect_leaf_fields(xml_text: str) -> dict:
    """Flatten every leaf element into {local_tag: text}, ignoring namespace prefixes."""
    # A SOAP response never needs a DTD; refusing one outright rules out
    # entity-expansion ("billion laughs") and external-entity tricks from a
    # spoofed or compromised endpoint.
    if "<!DOCTYPE" in xml_text.upper():
        raise AfsError("Refusing to parse an AFS response that contains a DTD.")
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
    approved = is_web_success(web_status) and (
        resp_code.strip() in ("00", "000") or resp_status.strip() == "1"
    )
    web_error = fields.get("WebResponseErrorDesc", "")
    cancelled = not approved and not resp_code.strip() and "cancel" in web_error.lower()

    return AfsResult(
        approved=approved,
        cancelled=cancelled,
        web_response_status=web_status,
        web_response_error=web_error,
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


def build_enquiry_by_ref_envelope(config: AfsConfig, orig_auth_code: str, orig_invoice_number: str, orig_rrn: str, printer: AfsPrinter) -> str:
    return _envelope("EnquiryByRef", _orig_ref_body(config, orig_auth_code, orig_invoice_number, orig_rrn, printer))


def build_settlement_envelope(config: AfsConfig) -> str:
    return _envelope("Settlement", config.to_xml())


def build_cancellation_envelope(config: AfsConfig) -> str:
    return _envelope("RequestCancellation", config.to_xml())


def classify(result: AfsResult) -> str:
    """Outcome per spec §10 / the C# sample: WebResponseStatus first (Success /
    Faild = the web call itself), then PosRespStatus (-1 unknown, 0 declined,
    1 approved). A 0 with no PosRespCode at all is NOT a decline: that is what
    AFS returns when it has no record (e.g. EnquiryByRef for a reference it
    never saw), so it stays 'unknown'."""
    if result.approved:
        return "approved"
    if result.cancelled:
        return "cancelled"
    if is_web_success(result.web_response_status) and result.pos_resp_status.strip() == "0" and result.pos_resp_code.strip():
        return "declined"
    return "unknown"


def correlate_recovery(result: AfsResult, expected_amount: Decimal, sale_started_at: datetime, skew_seconds: int = 60) -> bool:
    """After a dropped Sale connection: trust an approval found by EnquiryByRef only if amount AND timestamp both match; else unknown.

    ``result.pos_datetime`` and ``sale_started_at`` must be in the same
    timezone — the Odoo layer converts the terminal's local time to UTC first
    (afs.terminal._parse_response)."""
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
        # Callers build the envelope with build_*_envelope() first, so it can be
        # committed to the DB before the (up to the terminal timeout) blocking call.
        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": f"{SOAP_ACTION_BASE}{operation}",
        }
        # No redirects: a 307/308 would re-send the body, MerchantSecureKey
        # included, to whatever host the redirect names.
        resp = requests.post(self.endpoint_url, data=envelope.encode("utf-8"), headers=headers,
                             timeout=self.timeout, allow_redirects=False)
        if 300 <= resp.status_code < 400:
            raise AfsError(f"AFS endpoint answered with a redirect (HTTP {resp.status_code}); refusing to follow it.")
        resp.raise_for_status()
        return resp.text
