import json
from types import SimpleNamespace

import pytest

from parcelaudit.audit import run_audit
from parcelaudit.ingest.common import parse_date, parse_number
from parcelaudit.ingest.invoices import MappingError, parse_invoice_csv
from parcelaudit.ingest.shipments import parse_shipments_csv
from parcelaudit.models import Charge, InvoiceLine, Shipment
from parcelaudit.pii import mask_rows, mask_text
from parcelaudit.ratecard import RateCard, load_ratecard
from parcelaudit.sample import SAMPLE_RATECARD, generate


@pytest.fixture
def rc():
    return RateCard.model_validate(SAMPLE_RATECARD)


@pytest.mark.parametrize("raw,expected", [
    ("4,10", 4.10), ("1.234,56 €", 1234.56), ("1,234.56", 1234.56), ("-3.5", -3.5),
    ("EUR 12", 12.0), ("", None), ("abc", None), ("1.234.567", 1234567.0),
])
def test_parse_number(raw, expected):
    assert parse_number(raw) == expected


def test_parse_date():
    assert str(parse_date("03.09.2026")) == "2026-09-03"
    assert str(parse_date("2026-09-03T10:00:00")) == "2026-09-03"
    assert parse_date("garbage") is None


def test_demo_finds_every_injected_error(tmp_path, rc):
    paths = generate(tmp_path)
    lines, warnings = parse_invoice_csv(paths["invoice"].read_bytes(), load_ratecard(paths["ratecard"]))
    ships = parse_shipments_csv(paths["shipments"].read_bytes())
    result = run_audit(lines, rc, ships, warnings)
    counts = {k: v["count"] for k, v in result.by_rule().items()}
    for rule, n in paths["_expected"].items():
        assert counts.get(rule, 0) == n, rule
    assert not result.warnings


def test_clean_invoice_has_no_findings(rc):
    line = InvoiceLine(invoice_number="A", tracking_number="T1", service="Paket National", billed_weight_kg=1.5,
                       base_charge=3.90, surcharges=[Charge(name="Treibstoffzuschlag", amount=0.16),
                                                     Charge(name="Maut", amount=0.07)])
    ship = Shipment(tracking_number="T1", weight_kg=1.5)
    assert run_audit([line], rc, [ship]).findings == []


def test_weight_rule_respects_tolerance_and_bands(rc):
    # billed 2.05 vs real 2.0: within 0.1 tolerance → no finding
    l1 = InvoiceLine(tracking_number="T1", service="Paket", billed_weight_kg=2.05, base_charge=4.40)
    # billed 4.8 vs real 3.0: same band → no money lost → no finding
    l2 = InvoiceLine(tracking_number="T2", service="Paket", billed_weight_kg=4.8, base_charge=4.40)
    # billed 6 vs real 4: band 5→10 → 1.20 overcharge
    l3 = InvoiceLine(tracking_number="T3", service="Paket", billed_weight_kg=6, base_charge=5.60)
    ships = [Shipment(tracking_number="T1", weight_kg=2.0), Shipment(tracking_number="T2", weight_kg=3.0),
             Shipment(tracking_number="T3", weight_kg=4.0)]
    res = run_audit([l1, l2, l3], rc, ships)
    assert [(f.tracking_number, f.amount) for f in res.findings] == [("T3", 1.20)]


def test_volumetric_weight_is_fair_billing(rc):
    rc.volumetric_divisor = 5000
    line = InvoiceLine(tracking_number="T1", service="Paket", billed_weight_kg=6, base_charge=5.60)
    ship = Shipment(tracking_number="T1", weight_kg=1.0, length_cm=60, width_cm=40, height_cm=15)  # 7.2 kg vol
    assert run_audit([line], rc, [ship]).findings == []


def test_percent_surcharge_and_unknown_surcharge(rc):
    line = InvoiceLine(invoice_number="A", tracking_number="T1", service="Paket", billed_weight_kg=1,
                       base_charge=3.90, surcharges=[Charge(name="Fuel surcharge", amount=0.30),
                                                     Charge(name="Peak Season", amount=0.50)])
    rules = {f.rule: f.amount for f in run_audit([line], rc).findings}
    assert rules == {"surcharge_overcharged": round(0.30 - 3.90 * 0.04, 2), "surcharge_not_in_contract": 0.50}


def test_duplicate_line_suppresses_other_findings_on_it(rc):
    a = InvoiceLine(invoice_number="A", tracking_number="T1", service="Paket", billed_weight_kg=1, base_charge=4.50)
    b = InvoiceLine(invoice_number="B", tracking_number="T1", service="Paket", billed_weight_kg=1, base_charge=4.50)
    res = run_audit([a, b], rc)
    assert sorted((f.rule, f.invoice_number) for f in res.findings) == [
        ("duplicate_charge", "B"), ("rate_mismatch", "A")]


def test_claims_not_counted_as_overbilling(rc):
    line = InvoiceLine(tracking_number="T1", service="Paket", billed_weight_kg=1, base_charge=3.90)
    ship = Shipment(tracking_number="T1", weight_kg=1, status="lost", declared_value=900)
    res = run_audit([line], rc, [ship])
    assert res.claims_value() == 500  # liability cap
    assert res.recoverable() == 0


def test_wide_format_invoice(rc):
    csv = ("Sendungsnummer,Produkt,Gewicht,Grundpreis,Treibstoffzuschlag,Sperrgutzuschlag,Empfänger Name\n"
           "T1,Paket,1.0,3.90,0.16,,Max Mustermann\n"
           "T2,Paket,1.0,3.90,0.16,25.00,Erika Muster\n")
    lines, _ = parse_invoice_csv(csv, rc)
    assert [l.total for l in lines] == [4.06, 29.06]
    res = run_audit(lines, rc)
    assert [(f.rule, f.amount) for f in res.findings] == [("surcharge_overcharged", 7.0)]


def test_unmappable_invoice_raises(rc):
    with pytest.raises(MappingError):
        parse_invoice_csv("foo;bar\n1;2\n", rc)


def test_explicit_mapping(rc):
    csv = "X;Y;Z\nT1;1,0;3,90\n"
    lines, _ = parse_invoice_csv(csv, rc, {"tracking_number": "X", "billed_weight_kg": "Y", "base_charge": "Z"})
    assert lines[0].tracking_number == "T1" and lines[0].base_charge == 3.90


def test_pii_masking():
    rows = mask_rows(["Empfänger Name", "Sendungsnummer", "Notiz"],
                     [{"Empfänger Name": "Max", "Sendungsnummer": "T1", "Notiz": "max@ex.de, +49 170 1234567"}])
    assert rows[0]["Empfänger Name"] == "[REDACTED]"
    assert rows[0]["Sendungsnummer"] == "T1"
    assert "@" not in rows[0]["Notiz"] and "1234567" not in rows[0]["Notiz"]
    assert "[STREET]" in mask_text("Lieferung an Hauptstraße 12, Berlin")


def test_llm_mapping_with_mocked_client(monkeypatch):
    from parcelaudit import llm

    sent = {}

    class FakeStream:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            payload = {"tracking_number": "ParcelID", "amount": "EUR", "charge_type": None}
            return SimpleNamespace(stop_reason="end_turn",
                                   content=[SimpleNamespace(type="text", text=json.dumps(payload))])

    class FakeClient:
        beta = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: (sent.update(kw), FakeStream())[1]))

    monkeypatch.setattr(llm, "_client", lambda: FakeClient())
    m = llm.suggest_mapping(["ParcelID", "EUR", "Customer Name"],
                            [{"ParcelID": "T1", "EUR": "4", "Customer Name": "Max Mustermann"}],
                            {"tracking_number": [], "amount": [], "charge_type": []})
    assert m == {"tracking_number": "ParcelID", "amount": "EUR"}
    prompt = sent["messages"][0]["content"]
    assert "Max Mustermann" not in prompt  # PII never leaves
    assert sent["fallbacks"] == "default"


def test_web_upload(tmp_path):
    from fastapi.testclient import TestClient
    from parcelaudit.web import app

    paths = generate(tmp_path, n=50)
    client = TestClient(app)
    assert client.get("/").status_code == 200
    r = client.post("/audit", data={"customer": "Test GmbH"}, files=[
        ("ratecard", ("rc.yaml", paths["ratecard"].read_bytes())),
        ("invoices", ("inv.csv", paths["invoice"].read_bytes())),
        ("shipments", ("ship.csv", paths["shipments"].read_bytes())),
    ])
    assert r.status_code == 200 and "Overbilled" in r.text and "findings.csv" in r.text
    bad = client.post("/audit", files=[("ratecard", ("rc.yaml", b"nope: [")),
                                       ("invoices", ("inv.csv", b"a,b\n1,2\n"))])
    assert bad.status_code == 400
