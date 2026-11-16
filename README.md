# ParcelAudit

Audits parcel carrier invoices (DHL, DPD, GLS, Hermes, UPS…) against the shop's contract
rate card and its own shipment data. Outputs a customer report, a findings CSV and a
German dispute email that are ready to send to the carrier.

**Principle:** Claude reads messy inputs (unknown CSV layouts, PDF invoices, contract PDFs).
Every euro is calculated by deterministic Python rules, and each finding cites its evidence.

## Quick start

```bash
pip install -e '.[web,llm,dev]'
parcelaudit demo demo_out          # synthetic data with planted errors → demo_out/report.html
pytest -q
```

## The free-audit workflow (Phase 0, run from the CLI)

1. Get from the customer: carrier invoice export (CSV preferred) for the last 3 months, the contract
   or price sheet, and a shipment/label export (Shopify, Shopware, Billbee, Sendcloud…).
2. Rate card: `parcelaudit draft-ratecard contract.pdf -o customers/acme/ratecard.yaml`
   (uses Claude). **Check every price against the contract.** Open questions are listed at the top.
   You can also copy `examples/ratecard.demo.yaml` and fill it in by hand.
3. Audit:
   ```bash
   parcelaudit audit -r customers/acme/ratecard.yaml \
       -i customers/acme/invoice_jul.csv -i customers/acme/invoice_aug.csv \
       -s customers/acme/shipments.csv --customer "Acme GmbH" --period "Jul–Sep 2026" \
       -o customers/acme/out
   ```
   If columns aren't recognised, either add `--use-llm` (Claude proposes a mapping from the headers plus
   5 PII-masked sample rows) or write a mapping file (see `examples/mapping.example.yaml`).
   PDF invoices need `--use-llm`.
4. Send `report.html` (print to PDF) to the customer. They forward `dispute_email.txt` and
   `findings.csv` to the carrier.

## Checks

| Rule | What it catches | Confidence |
|---|---|---|
| `duplicate_charge` | Same parcel billed twice (same or different invoice) | high |
| `rate_mismatch` | Base price above contract price for the billed weight | high |
| `surcharge_overcharged` | Flat or % surcharge above the contract value (e.g. fuel %) | high |
| `weight_overbilled` | Billed weight in a pricier band than the real (or volumetric) weight | medium |
| `surcharge_not_in_contract` | Surcharge with no contract basis | medium |
| `claim_candidate` | Parcel lost or damaged according to shop data; value capped at liability limit | medium |
| `unknown_parcel` | Billed parcel missing from shop export (cancelled label? other account?) | low |

"Overbilled" counts invoice errors only. Lost-parcel claims are reported separately because they are
goods value, not overbilling. To add a rule, write a function in `parcelaudit/rules.py` and add it to `RULES`.

## Web app (Phase 1)

```bash
PARCELAUDIT_PASSWORD=secret uvicorn parcelaudit.web:app --port 8000
# or: docker build -t parcelaudit . && docker run -p 8000:8000 -e PARCELAUDIT_PASSWORD=secret parcelaudit
```
Upload the rate card, invoice CSVs and shipment CSV to get the report with download links.
Files are processed in memory only and nothing is stored. A Hetzner CX22 (~€5/month) is enough.
Put it behind HTTPS (Caddy) before customers use it.

## GDPR notes

- Invoices and shipment exports contain recipient personal data. Sign a data processing agreement
  (AVV) with every customer, and host in the EU.
- Before any Claude call, `pii.py` masks name, address and contact columns and email, phone and
  street patterns. CSV mapping sends only headers and 5 masked rows.
- PDF invoice extraction sends the PDF's masked **text**. Masking free text is best-effort, so
  recipient names can remain. Prefer CSV exports, and list Anthropic as a sub-processor in your AVV.
- Contract PDFs (for `draft-ratecard`) are business data and are sent whole.

## Claude configuration

`ANTHROPIC_API_KEY` (or `ant auth login`). `PARCELAUDIT_MODEL` defaults to `claude-opus-5-5`, and
`PARCELAUDIT_EFFORT` defaults to `medium`. Requests use structured JSON output, streaming, and
server-side model fallback (`fallbacks: "default"`) if a request is declined.

## Known limitations / next steps

- Real carrier exports vary. Collect 2–3 anonymised real files per carrier during the free audits,
  turn them into test fixtures, and add synonyms to `ingest/invoices.py`.
- Zones and countries: rate cards are per service, not per destination zone yet. For international
  zone pricing, add a `zone` key to services.
- Rate cards have no validity dates, and mid-period price changes such as fuel % updating monthly
  aren't modelled yet.
- No database or multi-tenant accounts yet. Add Postgres and logins once there are about 10 paying customers.
- Scanned PDFs (no text layer) aren't supported. Ask for the CSV export.
