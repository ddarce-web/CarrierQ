"""Remove personal data before anything leaves the machine (GDPR: data minimisation).

The audit itself needs only tracking number, weight, service and charges, so
recipient names/addresses/contacts are masked before any LLM call.
"""

from __future__ import annotations

import re

from .ratecard import norm

PII_HEADER_HINTS = ("name", "empfaenger", "recipient", "consignee", "strasse", "street", "address", "adresse",
                    "email", "mail", "telefon", "phone", "tel", "hausnummer", "housenumber", "city", "ort",
                    "firma", "company", "kontakt", "contact")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?<!\d)(?:\+|00)\d[\d /-]{7,}\d")
_STREET = re.compile(r"\b[\wäöüß.-]+(?:straße|strasse|str\.|weg|allee|platz|gasse|ring|damm)\s*\d+\w?\b", re.I)


def is_pii_header(header: str) -> bool:
    n = norm(header)
    return any(h in n for h in PII_HEADER_HINTS) and not any(k in n for k in ("produkt", "product", "service"))


def mask_text(text: str) -> str:
    text = _EMAIL.sub("[EMAIL]", text)
    text = _PHONE.sub("[PHONE]", text)
    return _STREET.sub("[STREET]", text)


def mask_rows(headers: list[str], rows: list[dict[str, str]]) -> list[dict[str, str]]:
    pii_cols = {h for h in headers if is_pii_header(h)}
    return [{k: ("[REDACTED]" if k in pii_cols and v else mask_text(v)) for k, v in row.items()} for row in rows]
