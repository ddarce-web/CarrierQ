"""Orchestration: inputs in, AuditResult out."""

from __future__ import annotations

from .models import AuditResult, InvoiceLine, Shipment
from .ratecard import RateCard
from .rules import Context, run_rules


def run_audit(lines: list[InvoiceLine], ratecard: RateCard, shipments: list[Shipment] | None = None,
              warnings: list[str] | None = None) -> AuditResult:
    warnings = list(warnings or [])
    ship_map = {s.tracking_number: s for s in (shipments or [])}

    unknown_services = sorted({l.service for l in lines if l.service and not ratecard.find_service(l.service)})
    if unknown_services:
        warnings.append("Services not in rate card (prices not checked): " + ", ".join(unknown_services[:10]))
    no_weight = sum(1 for l in lines if l.billed_weight_kg is None)
    if no_weight:
        warnings.append(f"{no_weight} invoice lines have no billed weight (rate and weight checks skipped).")
    if not ship_map:
        warnings.append("No shipment data supplied: weight, unknown-parcel and claim checks skipped.")
    elif lines:
        matched = sum(1 for l in lines if l.tracking_number in ship_map)
        if matched / len(lines) < 0.5:
            warnings.append(f"Only {matched}/{len(lines)} invoice lines match shipment data: "
                            "check that both files cover the same period/account.")

    findings = run_rules(Context(lines=lines, ratecard=ratecard, shipments=ship_map))
    return AuditResult(
        carrier=ratecard.carrier,
        lines_audited=len(lines),
        total_billed=round(sum(l.total for l in lines), 2),
        findings=findings,
        warnings=warnings,
    )
