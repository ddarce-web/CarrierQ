"""Outputs: HTML report, findings CSV (attach to disputes), dispute email draft."""

from __future__ import annotations

import csv
import io
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .models import AuditResult, Confidence

_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(["html"]),
)
_env.filters["eur"] = lambda v: f"€{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def render_html(result: AuditResult, customer: str = "", period: str = "") -> str:
    pct = result.overbilled_pct()
    return _env.get_template("report.html").render(
        r=result, customer=customer, period=period, today=date.today().isoformat(),
        recoverable=result.recoverable(), recoverable_all=result.recoverable(Confidence.low), claims=result.claims_value(),
        pct=pct, by_rule=result.by_rule(),
    )


def findings_csv(result: AuditResult) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["rule", "tracking_number", "invoice_number", "amount_eur", "confidence", "explanation"])
    for f in result.findings:
        w.writerow([f.rule, f.tracking_number, f.invoice_number, f"{f.amount:.2f}".replace(".", ","),
                    f.confidence.value, f.explanation])
    return buf.getvalue()


TITLES_DE = {
    "duplicate_charge": "Doppelte Berechnung",
    "rate_mismatch": "Grundpreis über Vertragspreis",
    "weight_overbilled": "Abrechnungsgewicht höher als Versandgewicht",
    "surcharge_not_in_contract": "Nicht vereinbarter Zuschlag",
    "surcharge_overcharged": "Zuschlag über Vertragskonditionen",
    "unknown_parcel": "Sendung nicht von uns versendet",
}


def dispute_email(result: AuditResult, customer: str = "[Firma]", customer_number: str = "[Kundennummer]") -> str:
    """German dispute email for the carrier, covering high+medium findings except claims
    (claims go through the carrier's separate claim process)."""
    disputable = [f for f in result.findings
                  if f.rule != "claim_candidate" and f.confidence in (Confidence.high, Confidence.medium)]
    invoices = sorted({f.invoice_number for f in disputable if f.invoice_number})
    total = sum(f.amount for f in disputable)
    by_rule = {}
    for f in disputable:
        title = TITLES_DE.get(f.rule, f.title)
        by_rule.setdefault(title, [0, 0.0])
        by_rule[title][0] += 1
        by_rule[title][1] += f.amount
    lines = "\n".join(f"  - {t}: {n} Sendungen, {a:.2f} EUR".replace(".", ",") for t, (n, a) in by_rule.items())
    return f"""Betreff: Rechnungsreklamation {', '.join(invoices) or ''} – Kundennummer {customer_number}

Sehr geehrte Damen und Herren,

bei der Prüfung der oben genannten Rechnung(en) haben wir Abweichungen zu unseren vertraglich
vereinbarten Konditionen festgestellt:

{lines}

Gesamtbetrag der Reklamation: {f'{total:.2f}'.replace('.', ',')} EUR (netto)

Die betroffenen Sendungsnummern mit Einzelbegründung finden Sie in der beigefügten CSV-Datei.
Wir bitten um Prüfung und Gutschrift des genannten Betrags.

Mit freundlichen Grüßen
{customer}
"""
