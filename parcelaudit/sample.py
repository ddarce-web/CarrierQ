"""Synthetic demo data with known, injected errors. The prices are made up for
illustration and are not any carrier's real tariff."""

from __future__ import annotations

import csv
import random
from datetime import date, timedelta
from pathlib import Path

import yaml

SAMPLE_RATECARD = {
    "carrier": "DemoCarrier",
    "contract_id": "DEMO-2026",
    "weight_tolerance_kg": 0.1,
    "volumetric_divisor": None,
    "liability_cap": 500,
    "services": {
        "PAKET_NATIONAL": {
            "aliases": ["Paket National", "Paket", "V01PAK"],
            "weight_bands": [
                {"max_kg": 2, "price": 3.90}, {"max_kg": 5, "price": 4.40},
                {"max_kg": 10, "price": 5.60}, {"max_kg": 31.5, "price": 8.90},
            ],
        },
        "PAKET_EU": {
            "aliases": ["Paket International EU", "V53WPAK"],
            "weight_bands": [{"max_kg": 5, "price": 11.50}, {"max_kg": 10, "price": 14.90}, {"max_kg": 31.5, "price": 21.00}],
        },
    },
    "surcharges": {
        "fuel": {"aliases": ["Treibstoffzuschlag", "Kraftstoffzuschlag", "Fuel surcharge", "Energiezuschlag"],
                 "type": "percent", "value": 4.0},
        "toll": {"aliases": ["Mautzuschlag", "Maut", "Toll"], "type": "flat", "value": 0.07},
        "bulky": {"aliases": ["Sperrgutzuschlag", "Sperrgut", "Bulky goods"], "type": "flat", "value": 18.00},
    },
}

SERVICES = {"PAKET_NATIONAL": "Paket National", "PAKET_EU": "Paket International EU"}


def _price(service: str, kg: float) -> float:
    for b in SAMPLE_RATECARD["services"][service]["weight_bands"]:
        if kg <= b["max_kg"]:
            return b["price"]
    raise ValueError


def de(x: float) -> str:
    return f"{x:.2f}".replace(".", ",")


def generate(out_dir: str | Path, n: int = 400, seed: int = 7) -> dict[str, Path]:
    """Write ratecard.yaml, invoice.csv (long format, German), shipments.csv. Returns paths
    plus 'expected' counts of injected errors via the returned dict['_expected']."""
    rnd = random.Random(seed)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    start = date(2026, 9, 1)
    shipments, inv_rows = [], []
    expected = {"duplicate_charge": 0, "rate_mismatch": 0, "weight_overbilled": 0, "surcharge_not_in_contract": 0,
                "surcharge_overcharged": 0, "unknown_parcel": 0, "claim_candidate": 0}

    for i in range(n):
        tn = f"00340434{100000000 + i:010d}"
        svc = "PAKET_EU" if rnd.random() < 0.15 else "PAKET_NATIONAL"
        kg = round(rnd.choice([rnd.uniform(0.2, 2), rnd.uniform(2, 5), rnd.uniform(5, 12)]), 2)
        d = start + timedelta(days=rnd.randint(0, 29))
        status = "delivered"
        value = round(rnd.uniform(15, 250), 2)
        billed_kg = kg
        base = _price(svc, kg)
        fuel = round(base * 0.04, 2)
        extra: list[tuple[str, float]] = []

        roll = rnd.random()
        if roll < 0.04:  # weight overbilled into next band
            billed_kg = {2: 2.6, 5: 5.4, 10: 10.8, 31.5: 32}.get(
                next(b["max_kg"] for b in SAMPLE_RATECARD["services"][svc]["weight_bands"] if kg <= b["max_kg"]), kg)
            if billed_kg <= 31.5 and _price(svc, billed_kg) > _price(svc, kg):
                base = _price(svc, billed_kg)
                fuel = round(base * 0.04, 2)
                expected["weight_overbilled"] += 1
            else:
                billed_kg = kg
        elif roll < 0.07:  # wrong base price
            base = round(base + 0.35, 2)
            fuel = round(base * 0.04, 2)
            expected["rate_mismatch"] += 1
        elif roll < 0.10:  # fuel charged at 5.5% instead of 4%
            fuel = round(base * 0.055, 2)
            expected["surcharge_overcharged"] += 1
        elif roll < 0.12:  # surcharge not in contract
            extra.append(("Adresskorrektur", 6.50))
            expected["surcharge_not_in_contract"] += 1
        elif roll < 0.13:
            status = "lost"
            expected["claim_candidate"] += 1

        shipments.append({"Bestellnummer": f"SO-{20000 + i}", "Trackingnummer": tn, "Versanddatum": d.strftime("%d.%m.%Y"),
                          "Gewicht (kg)": de(kg), "Versandart": SERVICES[svc], "Status": status,
                          "Warenwert": de(value), "Empfänger Name": f"Kunde {i}", "Straße": f"Musterweg {i % 90 + 1}"})
        invoice_no = "RE-2026-0901" if d.day <= 15 else "RE-2026-0916"
        rows = [(SERVICES[svc], base), ("Treibstoffzuschlag", fuel), ("Mautzuschlag", 0.07)] + extra
        for label, amt in rows:
            inv_rows.append({"Rechnungsnummer": invoice_no, "Sendungsnummer": tn, "Versanddatum": d.strftime("%d.%m.%Y"),
                             "Produkt": SERVICES[svc], "Abrechnungsgewicht": de(billed_kg), "Leistungsart": label,
                             "Betrag netto": de(amt), "Empfängerland": "DE" if svc == "PAKET_NATIONAL" else "AT"})

    # duplicates: re-bill 3 parcels from invoice 1 on invoice 2
    for tn_row in [r for r in inv_rows if r["Rechnungsnummer"] == "RE-2026-0901"][:9]:
        dup = dict(tn_row, Rechnungsnummer="RE-2026-0916")
        inv_rows.append(dup)
    expected["duplicate_charge"] += 3
    # unknown parcels: billed but never shipped by the shop
    for j in range(4):
        d = start + timedelta(days=10 + j)
        for label, amt in (("Paket National", 4.40), ("Treibstoffzuschlag", 0.18), ("Mautzuschlag", 0.07)):
            inv_rows.append({"Rechnungsnummer": "RE-2026-0916", "Sendungsnummer": f"0034043499999990{j:02d}",
                             "Versanddatum": d.strftime("%d.%m.%Y"), "Produkt": "Paket National",
                             "Abrechnungsgewicht": "3,00", "Leistungsart": label, "Betrag netto": de(amt),
                             "Empfängerland": "DE"})
    expected["unknown_parcel"] += 4

    paths = {"ratecard": out / "ratecard.yaml", "invoice": out / "invoice.csv", "shipments": out / "shipments.csv"}
    paths["ratecard"].write_text(yaml.safe_dump(SAMPLE_RATECARD, sort_keys=False, allow_unicode=True), encoding="utf-8")
    for key, rows in (("invoice", inv_rows), ("shipments", shipments)):
        with open(paths[key], "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()), delimiter=";")
            w.writeheader()
            w.writerows(rows)
    paths["_expected"] = expected  # type: ignore[assignment]
    return paths
