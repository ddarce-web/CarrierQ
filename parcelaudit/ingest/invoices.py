"""Turn carrier invoice CSVs into normalized InvoiceLines.

Two layouts are supported:
  * long  – one row per charge (tracking, charge type, amount). Common in DHL/DPD/GLS exports.
  * wide  – one row per parcel with a base-charge column plus surcharge columns.

Header recognition uses German + English synonyms. If that fails, pass an
explicit mapping (YAML via --mapping) or let llm.suggest_mapping() propose one.
"""

from __future__ import annotations

from collections import OrderedDict

from ..models import Charge, InvoiceLine
from ..ratecard import RateCard, norm
from .common import map_headers, parse_date, parse_number, read_csv

INVOICE_SYNONYMS: dict[str, list[str]] = {
    "tracking_number": ["tracking_number", "tracking", "sendungsnummer", "sendungsnr", "paketnummer",
                        "paketnr", "parcel_number", "parcelnumber", "shipment_number", "trackingnr",
                        "identcode", "barcode", "awb"],
    "invoice_number": ["invoice_number", "rechnungsnummer", "rechnungsnr", "invoice_no", "invoice"],
    "invoice_date": ["invoice_date", "rechnungsdatum"],
    "ship_date": ["ship_date", "versanddatum", "einlieferungsdatum", "pickup_date", "abholdatum",
                  "shipping_date", "datum", "date"],
    "service": ["service", "produkt", "product", "leistung", "service_type", "produktname"],
    "billed_weight_kg": ["billed_weight_kg", "billed_weight", "abrechnungsgewicht", "gewicht_kg",
                         "gewicht", "weight_kg", "weight", "berechnetes_gewicht"],
    "charge_type": ["charge_type", "charge_description", "leistungsart", "position", "kostenart",
                    "beschreibung", "description", "charge"],
    "amount": ["amount", "betrag", "nettobetrag", "net_amount", "betrag_netto", "preis", "price"],
    "base_charge": ["base_charge", "grundpreis", "fracht", "freight", "base_price", "basispreis",
                    "transportkosten"],
    "dest_country": ["dest_country", "empfaengerland", "zielland", "land", "country", "destination_country"],
}

REQUIRED = ("tracking_number",)
BASE_WORDS = ("grundpreis", "fracht", "freight", "base", "basis", "transport", "paketpreis")
SURCHARGE_WORDS = ("zuschlag", "surcharge", "fee", "gebuehr", "gebühr", "entgelt", "maut", "toll")


class MappingError(ValueError):
    def __init__(self, message: str, headers: list[str]):
        super().__init__(message)
        self.headers = headers


def parse_invoice_csv(
    data: bytes | str,
    ratecard: RateCard,
    mapping: dict[str, str] | None = None,
) -> tuple[list[InvoiceLine], list[str]]:
    """Parse an invoice CSV. Returns (lines, warnings)."""
    headers, rows = read_csv(data)
    cols = mapping or map_headers(headers, INVOICE_SYNONYMS)
    missing = [f for f in REQUIRED if f not in cols]
    if missing:
        raise MappingError(f"Could not find column(s) {missing} in invoice headers {headers}", headers)
    long_format = "charge_type" in cols and "amount" in cols
    if not long_format and "base_charge" not in cols and "amount" in cols:
        cols["base_charge"] = cols["amount"]  # single price column per parcel
    if not long_format and "base_charge" not in cols:
        raise MappingError("Need either (charge_type + amount) or a base_charge column", headers)

    warnings: list[str] = []
    lines: OrderedDict[tuple[str, str], InvoiceLine] = OrderedDict()
    mapped_cols = set(cols.values())
    surcharge_cols = [] if long_format else [
        h for h in headers
        if h not in mapped_cols and (ratecard.find_surcharge(h) or any(w in norm(h) for w in map(norm, SURCHARGE_WORDS)))
    ]

    def get(row: dict, field: str) -> str:
        return row.get(cols[field], "") if field in cols else ""

    for i, row in enumerate(rows, start=2):  # row 1 = header
        tracking = get(row, "tracking_number").replace(" ", "")
        if not tracking:
            continue
        inv = get(row, "invoice_number")
        key = (inv, tracking)
        line = lines.get(key)
        if line is None:
            line = InvoiceLine(
                invoice_number=inv,
                invoice_date=parse_date(get(row, "invoice_date")),
                tracking_number=tracking,
                ship_date=parse_date(get(row, "ship_date")),
                service=get(row, "service"),
                billed_weight_kg=parse_number(get(row, "billed_weight_kg")),
                dest_country=get(row, "dest_country"),
                source_row=i,
            )
            lines[key] = line
        else:
            line.billed_weight_kg = line.billed_weight_kg or parse_number(get(row, "billed_weight_kg"))
            line.service = line.service or get(row, "service")

        if long_format:
            label = get(row, "charge_type")
            amount = parse_number(get(row, "amount"))
            if amount is None:
                warnings.append(f"Row {i}: unreadable amount '{get(row, 'amount')}'")
                continue
            if _is_base_label(label, ratecard):
                if line.base_charge:
                    # second base charge for the same parcel on the same invoice = duplicate
                    line.surcharges.append(Charge(name=f"DUPLICATE BASE: {label}", amount=amount))
                else:
                    line.base_charge = amount
                    if not line.service and ratecard.is_service_label(label):
                        line.service = label
            else:
                line.surcharges.append(Charge(name=label, amount=amount))
        else:
            base = parse_number(get(row, "base_charge"))
            line.base_charge = base or 0.0
            for h in surcharge_cols:
                amt = parse_number(row.get(h))
                if amt:
                    line.surcharges.append(Charge(name=h, amount=amt))
    return list(lines.values()), warnings


def _is_base_label(label: str, ratecard: RateCard) -> bool:
    if ratecard.is_service_label(label):
        return True
    if ratecard.find_surcharge(label):
        return False
    n = norm(label)
    if any(norm(w) in n for w in SURCHARGE_WORDS):
        return False
    return any(w in n for w in BASE_WORDS)
