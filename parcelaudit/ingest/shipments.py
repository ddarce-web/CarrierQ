"""Shop-side shipment data (label export from Shopify/Shopware/Billbee/Sendcloud…)."""

from __future__ import annotations

from ..models import Shipment
from .common import map_headers, parse_date, parse_number, read_csv
from .invoices import MappingError

SHIPMENT_SYNONYMS: dict[str, list[str]] = {
    "tracking_number": ["tracking_number", "tracking", "sendungsnummer", "trackingnummer", "paketnummer",
                        "tracking_code", "trackingcode", "identcode"],
    "order_id": ["order_id", "order", "bestellnummer", "auftragsnummer", "order_number"],
    "ship_date": ["ship_date", "versanddatum", "shipped_at", "created_at", "datum", "date"],
    "weight_kg": ["weight_kg", "gewicht_kg", "gewicht", "weight", "package_weight"],
    "length_cm": ["length_cm", "laenge_cm", "laenge", "length"],
    "width_cm": ["width_cm", "breite_cm", "breite", "width"],
    "height_cm": ["height_cm", "hoehe_cm", "hoehe", "height"],
    "service": ["service", "versandart", "shipping_method", "produkt", "product"],
    "status": ["status", "sendungsstatus", "delivery_status", "tracking_status"],
    "declared_value": ["declared_value", "warenwert", "order_value", "value", "bestellwert"],
}


def parse_shipments_csv(data: bytes | str, mapping: dict[str, str] | None = None) -> list[Shipment]:
    headers, rows = read_csv(data)
    cols = mapping or map_headers(headers, SHIPMENT_SYNONYMS)
    if "tracking_number" not in cols:
        raise MappingError(f"No tracking column found in shipment headers {headers}", headers)

    def get(row, field):
        return row.get(cols[field], "") if field in cols else ""

    out: list[Shipment] = []
    for row in rows:
        tn = get(row, "tracking_number").replace(" ", "")
        if not tn:
            continue
        weight = parse_number(get(row, "weight_kg"))
        if weight is not None and "weight_kg" in cols and "g" == cols["weight_kg"].lower().rstrip(")").split("(")[-1].strip():
            weight /= 1000  # column labelled in grams, e.g. "Gewicht (g)"
        out.append(Shipment(
            tracking_number=tn,
            order_id=get(row, "order_id"),
            ship_date=parse_date(get(row, "ship_date")),
            weight_kg=weight,
            length_cm=parse_number(get(row, "length_cm")),
            width_cm=parse_number(get(row, "width_cm")),
            height_cm=parse_number(get(row, "height_cm")),
            service=get(row, "service"),
            status=get(row, "status").lower(),
            declared_value=parse_number(get(row, "declared_value")),
        ))
    return out
