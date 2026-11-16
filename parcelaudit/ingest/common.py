"""CSV reading and value parsing that copes with German/European exports."""

from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime

from ..ratecard import norm


def read_csv(data: bytes | str) -> tuple[list[str], list[dict[str, str]]]:
    """Read a CSV with unknown encoding and delimiter. Returns (headers, rows)."""
    if isinstance(data, bytes):
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
    else:
        text = data
    sample = text[:5000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ";" if sample.count(";") > sample.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    headers = [h.strip() for h in (reader.fieldnames or [])]
    rows = []
    for raw in reader:
        rows.append({(k or "").strip(): (v or "").strip() for k, v in raw.items() if k is not None})
    return headers, rows


_NUM_CLEAN = re.compile(r"[^\d,.\-]")


def parse_number(value: str | None) -> float | None:
    """Parse '1.234,56 €', '1,234.56', '4,10', '-3.5' → float."""
    if value is None:
        return None
    s = _NUM_CLEAN.sub("", str(value))
    if not s or s in {"-", ".", ","}:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):  # German: 1.234,56
            s = s.replace(".", "").replace(",", ".")
        else:  # English: 1,234.56
            s = s.replace(",", "")
    elif "," in s:
        # European exports use the comma as decimal separator ('4,10').
        if s.count(",") > 1:  # '1,234,567' → thousands separators
            s = s.replace(",", "")
        else:
            s = s.replace(",", ".")
    elif s.count(".") > 1:  # '1.234.567' → thousands separators
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


_DATE_FORMATS = ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%d.%m.%y", "%Y%m%d", "%d-%m-%Y", "%m/%d/%Y")


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    v = value.strip().split(" ")[0].split("T")[0]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def map_headers(headers: list[str], synonyms: dict[str, list[str]]) -> dict[str, str]:
    """Map canonical field → actual header using synonym lists (exact, then substring)."""
    normalized = {h: norm(h) for h in headers}
    mapping: dict[str, str] = {}
    used: set[str] = set()
    # exact matches first, so 'Gewicht' doesn't get stolen by a substring rule
    for field, syns in synonyms.items():
        targets = {norm(s) for s in syns}
        for h, nh in normalized.items():
            if h not in used and nh in targets:
                mapping[field] = h
                used.add(h)
                break
    for field, syns in synonyms.items():
        if field in mapping:
            continue
        targets = [norm(s) for s in syns if len(norm(s)) >= 4]
        for h, nh in normalized.items():
            if h not in used and any(t in nh for t in targets):
                mapping[field] = h
                used.add(h)
                break
    return mapping
