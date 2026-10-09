# afs_pos_integration

Odoo 18 module: take an AFS payment-terminal card payment directly from a
posted customer invoice.

## Setup

1. **Accounting → Configuration → Journals** — create a dedicated **Bank**
   journal for card clearing (e.g. "AFS Card Clearing", code `AFS`), pointed
   at a dedicated GL account. Leave the inbound payment method line's
   **Outstanding Receipts** account blank so paid invoices go straight to
   **Paid** instead of **In Payment** — verify this on staging against a real
   invoice before relying on it.
2. **Settings → General Settings → System Parameters** — add a key holding
   each terminal's `MerchantSecureKey`, e.g. `afs_pos.secure_key.reception_dubai`.
   Only Settings administrators (`base.group_system`) can read
   `ir.config_parameter` at all — that is core Odoo behaviour, not something
   this module adds.
3. **AFS POS → Terminals** — create one `afs.terminal` record per physical
   terminal: MID, TID, Tenant, the secure-key parameter name above, the
   clearing journal, and the settlement currency. Use **Test Connection**
   (an `Enquiry` call) to confirm credentials before going live.
4. **Settings → AFS POS Integration** — optionally set a default terminal.
   **Auto-Void on Credit Note Post** is on by default: it's what makes
   **Reverse Card Payment** actually reverse the card charge (see below).
   Switch it off only if you want credit notes against AFS-paid invoices to
   never touch the terminal.

## Using it

- On a posted customer invoice/receipt: **Pay by Card** → pick a terminal
  (defaults to the invoice's) → amount defaults to the residual → **Charge
  Card**. The wizard closes immediately — the actual `Sale` call runs in a
  background thread rather than blocking the request, so the invoice stays
  interactive while the cardholder taps. A banner shows "Waiting for the
  customer to tap or insert their card…" with a real, clickable **Cancel
  Payment** button (calls AFS's `RequestCancellation`), and the outcome is
  pushed to the browser over Odoo's bus the moment it resolves — no manual
  refresh needed. A cron sweep (every 5 minutes) is a safety net only: it
  flags anything still `pending` well past its terminal's timeout as
  **Unknown**, in case a worker died mid-transaction; it is not how the
  normal flow resolves.
- **Reverse Card Payment** (on the invoice) opens the standard Odoo credit
  note wizard for that invoice — it does not touch the terminal by itself.
  Posting the resulting credit note is what triggers `Void` automatically,
  refunding the full original amount back to the card. The button disappears
  once the payment has actually been reversed. `Void` has no amount field, so
  a credit note for anything other than the full original amount is refused
  at posting time — partial refunds must be processed on the terminal or via
  AFS directly. Managers also have a **Direct Void (no Credit Note)** button
  on the transaction record itself, for the rare case where a credit note
  isn't wanted at all.
- Every attempt (approved, declined, or otherwise) is kept on **AFS POS →
  Transactions** — this is the audit trail for a card-payment dispute and is
  append-only by design.
- If a Sale's connection drops mid-call, the module does **not** retry. It
  recovers via `EnquiryByRef`, keyed by the unique reference generated for
  that attempt (not a blank `Enquiry`, which can't be tied to a specific
  attempt) — if that doesn't return a confirmed outcome, the transaction is
  marked **Unknown** with a banner telling the user not to retry and to check
  the terminal or AFS portal.

## Printer receipt options

`afs.terminal.print_pos_receipt` and `print_receipt_note` are enums per the
AFS spec, not booleans: the former is None/Merchant Copy/Customer Copy/Both
Copies, the latter positions a custom note before/after the header/footer.
Pick "Both Copies" if reception needs both a merchant and a customer receipt
printed automatically.

## Known open question

Whether `Void` succeeds once a batch has settled is unverified beyond
same-day, pre-settlement testing. `afs.terminal.void_requires_open_batch`
(default on) refuses Void once a Settlement has run since the sale; flip it
off only after confirming with AFS that Void works across settled batches.

## Running the tests

```
cd custom/afs_pos_integration/tests && python -m unittest test_client -v
```

`test_client.py` is pure Python (SOAP envelope building/parsing, redaction,
recovery correlation) and needs no database. `test_invoice_flow.py` needs a
real Odoo test database (`--test-enable`) since the wizard under test
deliberately performs real commits mid-flow (see the note at the top of that
file) — never point it at production data.
