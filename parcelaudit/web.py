"""Minimal web app: upload files → audit report. Nothing is stored on disk;
files live only in memory for the duration of the request (GDPR-friendly MVP).

Run:  PARCELAUDIT_PASSWORD=secret uvicorn parcelaudit.web:app --port 8000
"""

from __future__ import annotations

import base64
import os
import secrets

import yaml
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pathlib import Path
from pydantic import ValidationError

from .audit import run_audit
from .ingest.invoices import MappingError, parse_invoice_csv
from .ingest.shipments import parse_shipments_csv
from .ratecard import RateCard
from .report import dispute_email, findings_csv, render_html

app = FastAPI(title="ParcelAudit", docs_url=None, redoc_url=None)
security = HTTPBasic(auto_error=False)
_env = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"), autoescape=select_autoescape(["html"]))
MAX_UPLOAD = 25 * 1024 * 1024


def auth(creds: HTTPBasicCredentials | None = Depends(security)):
    password = os.environ.get("PARCELAUDIT_PASSWORD")
    if not password:
        return  # local use
    if not creds or not secrets.compare_digest(creds.password.encode(), password.encode()):
        raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Basic"})


async def _read(f: UploadFile) -> bytes:
    data = await f.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, f"{f.filename} is larger than 25 MB")
    return data


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(auth)])
def index():
    return _env.get_template("upload.html").render(error=None)


@app.get("/healthz", response_class=PlainTextResponse)
def health():
    return "ok"


@app.post("/audit", response_class=HTMLResponse, dependencies=[Depends(auth)])
async def audit(
    ratecard: UploadFile = File(...),
    invoices: list[UploadFile] = File(...),
    shipments: UploadFile | None = File(None),
    customer: str = Form(""),
    period: str = Form(""),
):
    def fail(msg: str):
        return HTMLResponse(_env.get_template("upload.html").render(error=msg), status_code=400)

    try:
        rc = RateCard.model_validate(yaml.safe_load(await _read(ratecard)))
    except (yaml.YAMLError, ValidationError, TypeError) as e:
        return fail(f"Rate card could not be read: {e}")

    lines, warnings = [], []
    for f in invoices:
        if (f.filename or "").lower().endswith(".pdf"):
            return fail(f"{f.filename}: PDF invoices are handled via the CLI with --use-llm for now; upload the CSV export.")
        try:
            ls, ws = parse_invoice_csv(await _read(f), rc)
        except MappingError as e:
            return fail(f"{f.filename}: {e}")
        lines += ls
        warnings += [f"{f.filename}: {w}" for w in ws]

    ships = None
    if shipments and shipments.filename:
        try:
            ships = parse_shipments_csv(await _read(shipments))
        except MappingError as e:
            return fail(f"{shipments.filename}: {e}")

    result = run_audit(lines, rc, ships, warnings)
    html = render_html(result, customer, period)
    csv_b64 = base64.b64encode(findings_csv(result).encode("utf-8-sig")).decode()
    mail_b64 = base64.b64encode(dispute_email(result, customer or "[Firma]").encode()).decode()
    downloads = (
        '<div style="max-width:1040px;margin:16px auto 0;padding:0 16px;font:14px system-ui">'
        f'<a download="findings.csv" href="data:text/csv;base64,{csv_b64}">Download findings CSV</a> · '
        f'<a download="dispute_email.txt" href="data:text/plain;charset=utf-8;base64,{mail_b64}">Download dispute email</a> · '
        '<a href="/">New audit</a></div>'
    )
    return html.replace("<main>", downloads + "<main>", 1)
