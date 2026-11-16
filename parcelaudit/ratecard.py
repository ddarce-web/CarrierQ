"""Contract rate cards, stored as reviewed YAML.

The LLM may *draft* a rate card from a contract PDF (see llm.py), but a human
checks it before it is used: all prices the audit relies on come from here.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator


def norm(s: str) -> str:
    """Normalize labels for matching: lowercase, alphanumerics only."""
    return re.sub(r"[^a-z0-9]", "", s.lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss"))


class WeightBand(BaseModel):
    max_kg: float
    price: float


class Service(BaseModel):
    aliases: list[str] = Field(default_factory=list)
    weight_bands: list[WeightBand]

    @model_validator(mode="after")
    def _sort(self):
        self.weight_bands.sort(key=lambda b: b.max_kg)
        return self

    def price_for(self, weight_kg: float) -> float | None:
        for band in self.weight_bands:
            if weight_kg <= band.max_kg + 1e-9:
                return band.price
        return None  # over max weight

    def band_for(self, weight_kg: float) -> float | None:
        for band in self.weight_bands:
            if weight_kg <= band.max_kg + 1e-9:
                return band.max_kg
        return None


class Surcharge(BaseModel):
    aliases: list[str] = Field(default_factory=list)
    type: str = "flat"  # flat | percent (percent of base charge)
    value: float


class RateCard(BaseModel):
    carrier: str
    contract_id: str = ""
    currency: str = "EUR"
    weight_tolerance_kg: float = 0.1
    volumetric_divisor: float | None = None  # e.g. 5000 → L*W*H/5000 = kg; None = carrier bills real weight
    liability_cap: float = 500.0  # max claim value per parcel
    services: dict[str, Service]
    surcharges: dict[str, Surcharge] = Field(default_factory=dict)

    # ---- lookups -------------------------------------------------------
    def find_service(self, label: str) -> tuple[str, Service] | None:
        n = norm(label)
        for key, svc in self.services.items():
            if n == norm(key) or n in {norm(a) for a in svc.aliases}:
                return key, svc
        if len(self.services) == 1 and not n:
            return next(iter(self.services.items()))
        return None

    def find_surcharge(self, label: str) -> tuple[str, Surcharge] | None:
        n = norm(label)
        for key, sc in self.surcharges.items():
            if n == norm(key) or n in {norm(a) for a in sc.aliases}:
                return key, sc
        return None

    def is_service_label(self, label: str) -> bool:
        return self.find_service(label) is not None


def load_ratecard(path: str | Path) -> RateCard:
    with open(path, encoding="utf-8") as fh:
        return RateCard.model_validate(yaml.safe_load(fh))
