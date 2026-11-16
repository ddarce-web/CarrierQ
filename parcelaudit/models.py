"""Normalized data model. Every input format is converted into these types
before any rule runs, so rules never deal with carrier-specific quirks."""

from __future__ import annotations

from datetime import date
from enum import Enum

from pydantic import BaseModel, Field


class Charge(BaseModel):
    name: str
    amount: float


class InvoiceLine(BaseModel):
    """One billed shipment (all charges for one tracking number on one invoice)."""

    invoice_number: str = ""
    invoice_date: date | None = None
    tracking_number: str
    ship_date: date | None = None
    service: str = ""
    billed_weight_kg: float | None = None
    base_charge: float = 0.0
    surcharges: list[Charge] = Field(default_factory=list)
    dest_country: str = ""
    source_row: int | None = None  # row in the original file, for evidence

    @property
    def total(self) -> float:
        return round(self.base_charge + sum(s.amount for s in self.surcharges), 2)


class Shipment(BaseModel):
    """What the shop actually shipped (from label/order export)."""

    tracking_number: str
    ship_date: date | None = None
    weight_kg: float | None = None
    length_cm: float | None = None
    width_cm: float | None = None
    height_cm: float | None = None
    service: str = ""
    status: str = ""  # e.g. delivered, lost, damaged, returned
    declared_value: float | None = None
    order_id: str = ""


class Confidence(str, Enum):
    high = "high"  # contract/data clearly contradicts the invoice
    medium = "medium"  # very likely, worth disputing
    low = "low"  # needs a human look before disputing


class Finding(BaseModel):
    rule: str
    title: str
    tracking_number: str
    invoice_number: str = ""
    amount: float  # euros the customer was overcharged / can claim
    confidence: Confidence
    explanation: str
    evidence: dict = Field(default_factory=dict)


class AuditResult(BaseModel):
    carrier: str
    lines_audited: int
    total_billed: float
    findings: list[Finding]
    warnings: list[str] = Field(default_factory=list)

    def recoverable(self, min_confidence: Confidence = Confidence.medium) -> float:
        """Overbilled amount (invoice errors only; lost-parcel claims are reported separately)."""
        order = [Confidence.high, Confidence.medium, Confidence.low]
        allowed = order[: order.index(min_confidence) + 1]
        return round(sum(f.amount for f in self.findings
                         if f.confidence in allowed and f.rule != "claim_candidate"), 2)

    def claims_value(self) -> float:
        return round(sum(f.amount for f in self.findings if f.rule == "claim_candidate"), 2)

    def overbilled_pct(self) -> float:
        return self.recoverable() / self.total_billed * 100 if self.total_billed else 0.0

    def by_rule(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for f in self.findings:
            row = out.setdefault(f.rule, {"title": f.title, "count": 0, "amount": 0.0})
            row["count"] += 1
            row["amount"] = round(row["amount"] + f.amount, 2)
        return dict(sorted(out.items(), key=lambda kv: -kv[1]["amount"]))
