"""Claude-assisted steps. The LLM reads messy inputs; it never computes money.

  * suggest_mapping   – map unknown CSV headers to our fields (headers + masked samples only)
  * extract_pdf_invoice – PDF invoice → normalized lines (text is PII-masked first)
  * draft_ratecard    – contract PDF → rate card draft for HUMAN REVIEW

Needs ANTHROPIC_API_KEY (or `ant auth login`). Model is configurable with
PARCELAUDIT_MODEL.
"""

from __future__ import annotations

import base64
import io
import json
import os

from .ingest.common import parse_date
from .models import Charge, InvoiceLine
from .pii import mask_rows, mask_text

MODEL = os.environ.get("PARCELAUDIT_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("PARCELAUDIT_EFFORT", "medium")


class LLMError(RuntimeError):
    pass


def _client():
    try:
        import anthropic
    except ImportError as e:  # pragma: no cover
        raise LLMError("Install the llm extra: pip install -e '.[llm]'") from e
    return anthropic.Anthropic()


def _call_json(system: str, content: list[dict] | str, schema: dict, max_tokens: int = 16000) -> dict:
    import anthropic

    client = _client()
    try:
        # Stream so long extractions don't hit HTTP timeouts; server-side
        # fallback re-runs the request on another model if this one declines.
        with client.beta.messages.stream(
            model=MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": schema}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            msg = stream.get_final_message()
    except anthropic.RateLimitError as e:
        raise LLMError("Claude API rate limit hit; retry in a minute.") from e
    except anthropic.APIStatusError as e:
        raise LLMError(f"Claude API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise LLMError("Could not reach the Claude API.") from e

    if msg.stop_reason == "refusal":
        raise LLMError("The model declined this request.")
    if msg.stop_reason == "max_tokens":
        raise LLMError("Output was cut off (max_tokens); split the document into smaller parts.")
    text = next((b.text for b in msg.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMError("Model returned invalid JSON") from e


# --------------------------------------------------------------------------
def suggest_mapping(headers: list[str], rows: list[dict[str, str]], fields: dict[str, list[str]]) -> dict[str, str]:
    """Ask Claude which header corresponds to which canonical field."""
    sample = mask_rows(headers, rows[:5])
    props = {f: {"type": ["string", "null"], "enum": headers + [None]} for f in fields}
    schema = {"type": "object", "properties": props, "required": list(fields), "additionalProperties": False}
    prompt = (
        "Map the columns of this parcel-carrier CSV export to our fields.\n"
        f"Our fields (with example names): {json.dumps(fields, ensure_ascii=False)}\n"
        f"CSV headers: {json.dumps(headers, ensure_ascii=False)}\n"
        f"Sample rows (personal data redacted): {json.dumps(sample, ensure_ascii=False)}\n"
        "Use null when no column fits. Each header may be used at most once. "
        "'charge_type' + 'amount' apply only when there is one row per charge."
    )
    result = _call_json("You map spreadsheet columns precisely. Answer only with the requested JSON.", prompt, schema,
                        max_tokens=4000)
    mapping = {k: v for k, v in result.items() if v}
    if len(set(mapping.values())) != len(mapping):
        raise LLMError("Model mapped one header to several fields; supply a mapping file instead.")
    return mapping


# --------------------------------------------------------------------------
_LINE_SCHEMA = {
    "type": "object",
    "properties": {
        "invoice_number": {"type": "string"},
        "invoice_date": {"type": "string", "description": "YYYY-MM-DD or empty"},
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tracking_number": {"type": "string"},
                    "ship_date": {"type": "string"},
                    "service": {"type": "string"},
                    "billed_weight_kg": {"type": ["number", "null"]},
                    "base_charge": {"type": "number"},
                    "surcharges": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"name": {"type": "string"}, "amount": {"type": "number"}},
                            "required": ["name", "amount"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["tracking_number", "ship_date", "service", "billed_weight_kg", "base_charge", "surcharges"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["invoice_number", "invoice_date", "lines"],
    "additionalProperties": False,
}

_EXTRACT_SYSTEM = (
    "You extract parcel carrier invoice data exactly as printed. Never compute, round or infer amounts: "
    "copy net amounts in EUR as numbers (German '4,10' → 4.10). One entry per tracking number; put the "
    "main transport price in base_charge and every other charge (fuel, toll, bulky, peak, address "
    "correction…) in surcharges with its printed name. Skip totals, subtotals and VAT lines."
)


def extract_pdf_invoice(pdf_bytes: bytes, pages_per_chunk: int = 8) -> list[InvoiceLine]:
    """Extract invoice lines from a PDF. Text is pulled locally and PII-masked
    before sending; scanned (image-only) PDFs are not supported in this version."""
    try:
        from pypdf import PdfReader
    except ImportError as e:  # pragma: no cover
        raise LLMError("Install the llm extra: pip install -e '.[llm]'") from e
    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages = [mask_text(p.extract_text() or "") for p in reader.pages]
    if not any(p.strip() for p in pages):
        raise LLMError("PDF has no text layer (scanned?). Ask the customer for the CSV invoice export.")

    out: list[InvoiceLine] = []
    inv_no, inv_date = "", None
    for start in range(0, len(pages), pages_per_chunk):
        chunk = "\n\n--- page break ---\n\n".join(pages[start:start + pages_per_chunk])
        data = _call_json(_EXTRACT_SYSTEM, f"Invoice text (pages {start + 1}+):\n\n{chunk}", _LINE_SCHEMA)
        inv_no = data.get("invoice_number") or inv_no
        inv_date = parse_date(data.get("invoice_date")) or inv_date
        for row in data["lines"]:
            out.append(InvoiceLine(
                invoice_number=inv_no,
                invoice_date=inv_date,
                tracking_number=row["tracking_number"].replace(" ", ""),
                ship_date=parse_date(row.get("ship_date")),
                service=row.get("service", ""),
                billed_weight_kg=row.get("billed_weight_kg"),
                base_charge=row.get("base_charge") or 0.0,
                surcharges=[Charge(**s) for s in row.get("surcharges", [])],
            ))
    return out


# --------------------------------------------------------------------------
_RATECARD_SCHEMA = {
    "type": "object",
    "properties": {
        "carrier": {"type": "string"},
        "contract_id": {"type": "string"},
        "volumetric_divisor": {"type": ["number", "null"]},
        "liability_cap": {"type": "number"},
        "services": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "weight_bands": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"max_kg": {"type": "number"}, "price": {"type": "number"}},
                            "required": ["max_kg", "price"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["key", "aliases", "weight_bands"],
                "additionalProperties": False,
            },
        },
        "surcharges": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "type": {"type": "string", "enum": ["flat", "percent"]},
                    "value": {"type": "number"},
                },
                "required": ["key", "aliases", "type", "value"],
                "additionalProperties": False,
            },
        },
        "uncertainties": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["carrier", "contract_id", "volumetric_divisor", "liability_cap", "services", "surcharges",
                 "uncertainties"],
    "additionalProperties": False,
}


def draft_ratecard(contract_pdf: bytes) -> tuple[dict, list[str]]:
    """Draft a rate card from a carrier contract / price sheet PDF.
    Returns (ratecard_dict, uncertainties). A human must review before use."""
    content = [
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                        "data": base64.b64encode(contract_pdf).decode()}},
        {"type": "text", "text": (
            "Turn this parcel carrier contract / price list into a rate card. Use net EUR prices exactly as "
            "printed. For each product (service) list weight bands as upper limit in kg + price. List every "
            "surcharge with flat EUR amount or percent of base price. Add the invoice wording you'd expect "
            "for each as aliases. Use 500 as liability_cap if not stated. List anything ambiguous (zones, "
            "dimension rules, validity dates, conditional surcharges) in uncertainties."
        )},
    ]
    data = _call_json("You are a meticulous logistics contract analyst.", content, _RATECARD_SCHEMA)
    rc = {
        "carrier": data["carrier"],
        "contract_id": data["contract_id"],
        "volumetric_divisor": data["volumetric_divisor"],
        "liability_cap": data["liability_cap"],
        "services": {s["key"]: {"aliases": s["aliases"], "weight_bands": s["weight_bands"]} for s in data["services"]},
        "surcharges": {s["key"]: {"aliases": s["aliases"], "type": s["type"], "value": s["value"]}
                       for s in data["surcharges"]},
    }
    return rc, data["uncertainties"]
