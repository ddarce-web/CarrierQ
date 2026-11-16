"""Audit rules. Pure, deterministic Python: no LLM touches the money.

Each rule takes the audit context and yields Findings. Adding a rule means
writing one function and registering it in RULES.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .models import Confidence, Finding, InvoiceLine, Shipment
from .ratecard import RateCard

LOST_STATUSES = {"lost", "verloren", "missing", "vermisst", "damaged", "beschaedigt", "beschädigt"}
MONEY_EPS = 0.01


@dataclass
class Context:
    lines: list[InvoiceLine]
    ratecard: RateCard
    shipments: dict[str, Shipment] = field(default_factory=dict)

    @property
    def has_shipments(self) -> bool:
        return bool(self.shipments)


def _r(x: float) -> float:
    return round(x, 2)


# --------------------------------------------------------------------------
def duplicate_charges(ctx: Context) -> Iterable[Finding]:
    """Same parcel billed more than once (same or different invoice)."""
    seen: dict[str, InvoiceLine] = {}
    for line in ctx.lines:
        for s in line.surcharges:
            if s.name.startswith("DUPLICATE BASE:"):
                yield Finding(
                    rule="duplicate_charge", title="Duplicate charge",
                    tracking_number=line.tracking_number, invoice_number=line.invoice_number,
                    amount=_r(s.amount), confidence=Confidence.high,
                    explanation=f"Base charge billed twice on invoice {line.invoice_number}.",
                    evidence={"row": line.source_row},
                )
        first = seen.get(line.tracking_number)
        if first is None:
            seen[line.tracking_number] = line
            continue
        if line.base_charge > 0:
            yield Finding(
                rule="duplicate_charge", title="Duplicate charge",
                tracking_number=line.tracking_number, invoice_number=line.invoice_number,
                amount=_r(line.total), confidence=Confidence.high,
                explanation=(f"Parcel already billed on invoice {first.invoice_number or '?'} "
                             f"(row {first.source_row}); billed again here."),
                evidence={"first_invoice": first.invoice_number, "row": line.source_row},
            )


def _chargeable_weight(ship: Shipment, rc: RateCard) -> float | None:
    if ship.weight_kg is None:
        return None
    w = ship.weight_kg
    if rc.volumetric_divisor and ship.length_cm and ship.width_cm and ship.height_cm:
        w = max(w, ship.length_cm * ship.width_cm * ship.height_cm / rc.volumetric_divisor)
    return w


def weight_overbilled(ctx: Context) -> Iterable[Finding]:
    """Billed weight puts parcel in a higher (more expensive) band than its real weight."""
    if not ctx.has_shipments:
        return
    for line in ctx.lines:
        ship = ctx.shipments.get(line.tracking_number)
        svc = ctx.ratecard.find_service(line.service)
        if not ship or not svc or line.billed_weight_kg is None:
            continue
        real = _chargeable_weight(ship, ctx.ratecard)
        if real is None or line.billed_weight_kg <= real + ctx.ratecard.weight_tolerance_kg:
            continue
        _, service = svc
        billed_price = service.price_for(line.billed_weight_kg)
        fair_price = service.price_for(real)
        if billed_price is None or fair_price is None or billed_price - fair_price < MONEY_EPS:
            continue
        yield Finding(
            rule="weight_overbilled", title="Billed weight higher than shipped weight",
            tracking_number=line.tracking_number, invoice_number=line.invoice_number,
            amount=_r(billed_price - fair_price), confidence=Confidence.medium,
            explanation=(f"Billed as {line.billed_weight_kg:g} kg (band ≤{service.band_for(line.billed_weight_kg):g} kg, "
                         f"€{billed_price:.2f}) but shipped at {real:.2f} kg chargeable "
                         f"(band ≤{service.band_for(real):g} kg, €{fair_price:.2f})."),
            evidence={"billed_kg": line.billed_weight_kg, "shipped_kg": ship.weight_kg,
                      "chargeable_kg": round(real, 3), "row": line.source_row},
        )


def rate_mismatch(ctx: Context) -> Iterable[Finding]:
    """Base charge differs from the contract price for the billed weight."""
    for line in ctx.lines:
        svc = ctx.ratecard.find_service(line.service)
        if not svc or line.billed_weight_kg is None or line.base_charge <= 0:
            continue
        key, service = svc
        expected = service.price_for(line.billed_weight_kg)
        if expected is None:
            continue
        diff = line.base_charge - expected
        if diff > MONEY_EPS:
            yield Finding(
                rule="rate_mismatch", title="Base price above contract rate",
                tracking_number=line.tracking_number, invoice_number=line.invoice_number,
                amount=_r(diff), confidence=Confidence.high,
                explanation=(f"{key} at {line.billed_weight_kg:g} kg costs €{expected:.2f} per contract; "
                             f"billed €{line.base_charge:.2f}."),
                evidence={"contract_price": expected, "billed": line.base_charge, "row": line.source_row},
            )


def surcharges(ctx: Context) -> Iterable[Finding]:
    """Surcharges that aren't in the contract, or exceed the contracted amount."""
    for line in ctx.lines:
        for s in line.surcharges:
            if s.name.startswith("DUPLICATE BASE:") or s.amount <= 0:
                continue
            match = ctx.ratecard.find_surcharge(s.name)
            if match is None:
                yield Finding(
                    rule="surcharge_not_in_contract", title="Surcharge not in contract",
                    tracking_number=line.tracking_number, invoice_number=line.invoice_number,
                    amount=_r(s.amount), confidence=Confidence.medium,
                    explanation=f"'{s.name}' (€{s.amount:.2f}) is not an agreed surcharge in the rate card.",
                    evidence={"surcharge": s.name, "row": line.source_row},
                )
                continue
            key, sc = match
            allowed = sc.value if sc.type == "flat" else line.base_charge * sc.value / 100
            if s.amount - allowed > MONEY_EPS:
                yield Finding(
                    rule="surcharge_overcharged", title="Surcharge above contract",
                    tracking_number=line.tracking_number, invoice_number=line.invoice_number,
                    amount=_r(s.amount - allowed), confidence=Confidence.high,
                    explanation=(f"{key}: contract allows €{allowed:.2f}"
                                 f"{f' ({sc.value:g}% of €{line.base_charge:.2f})' if sc.type == 'percent' else ''}"
                                 f", billed €{s.amount:.2f}."),
                    evidence={"surcharge": s.name, "allowed": round(allowed, 2), "billed": s.amount,
                              "row": line.source_row},
                )


def unknown_parcels(ctx: Context) -> Iterable[Finding]:
    """Billed parcels that don't appear in the shop's shipment export."""
    if not ctx.has_shipments:
        return
    dates = [s.ship_date for s in ctx.shipments.values() if s.ship_date]
    lo, hi = (min(dates), max(dates)) if dates else (None, None)
    for line in ctx.lines:
        if line.tracking_number in ctx.shipments:
            continue
        if lo and line.ship_date and not (lo <= line.ship_date <= hi):
            continue  # outside export period: can't tell
        yield Finding(
            rule="unknown_parcel", title="Billed parcel not in your shipment data",
            tracking_number=line.tracking_number, invoice_number=line.invoice_number,
            amount=_r(line.total), confidence=Confidence.low,
            explanation=("No matching shipment in your export. Could be a cancelled label that was billed, "
                         "another account's parcel, or a return label: check before disputing."),
            evidence={"row": line.source_row},
        )


def claim_candidates(ctx: Context) -> Iterable[Finding]:
    """Parcels marked lost/damaged in shop data: claim value up to the liability cap."""
    if not ctx.has_shipments:
        return
    billed = {l.tracking_number for l in ctx.lines}
    for ship in ctx.shipments.values():
        if ship.status not in LOST_STATUSES or ship.tracking_number not in billed:
            continue
        value = min(ship.declared_value or 0.0, ctx.ratecard.liability_cap)
        if value <= 0:
            continue
        yield Finding(
            rule="claim_candidate", title="Lost/damaged parcel: file a claim",
            tracking_number=ship.tracking_number, amount=_r(value), confidence=Confidence.medium,
            explanation=(f"Status '{ship.status}', goods value €{ship.declared_value or 0:.2f} "
                         f"(carrier liability cap €{ctx.ratecard.liability_cap:.0f}). "
                         "Check the carrier's claim deadline."),
            evidence={"order_id": ship.order_id, "status": ship.status},
        )


RULES: list[Callable[[Context], Iterable[Finding]]] = [
    duplicate_charges,
    rate_mismatch,
    weight_overbilled,
    surcharges,
    unknown_parcels,
    claim_candidates,
]


def dedupe_overlaps(findings: list[Finding]) -> list[Finding]:
    """A duplicated parcel is refunded in full, so other findings on that same
    duplicate line would double-count. Keep the duplicate finding only."""
    dup_keys = {(f.tracking_number, f.invoice_number) for f in findings if f.rule == "duplicate_charge"
                and "first_invoice" in f.evidence}
    out = []
    for f in findings:
        if f.rule not in ("duplicate_charge", "claim_candidate") and (f.tracking_number, f.invoice_number) in dup_keys:
            continue
        out.append(f)
    return out


def run_rules(ctx: Context) -> list[Finding]:
    findings: list[Finding] = []
    for rule in RULES:
        findings.extend(rule(ctx))
    findings = dedupe_overlaps(findings)
    # weight_overbilled and rate_mismatch on the same line can overlap only if
    # both are true; that's two separate errors, so both stay.
    grouped: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:
        grouped[f.rule].append(f)
    return [f for fs in grouped.values() for f in sorted(fs, key=lambda x: -x.amount)]
