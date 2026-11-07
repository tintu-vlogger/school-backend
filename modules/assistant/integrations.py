"""Integration settings: what each connection needs, storing secrets encrypted, and simple
connection tests. Other modules read a connection with `get_config(db, company_id, kind)`
(secrets decrypted) or `smtp_config(...)`."""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone

from cryptography.fernet import Fernet
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from core import audit
from core.config import get_settings
from core.errors import BadRequest
from modules.assistant.models import IntegrationSetting


def _f(key, label, kind="text", secret=False, required=False, options=None, help="", placeholder=""):
    return {"key": key, "label": label, "type": kind, "secret": secret, "required": required,
            "options": options, "help": help, "placeholder": placeholder}


KINDS = {
    "ai_model": {
        "label": "AI model", "group": "AI",
        "description": "The model the data assistant uses. Any OpenAI-compatible service works (OpenAI, Azure "
                       "OpenAI, Groq, Together, a local Ollama or vLLM server), as do Anthropic and Google Gemini.",
        "fields": [
            _f("provider", "Provider", "select", required=True,
               options=[{"value": "openai", "label": "OpenAI-compatible"}, {"value": "anthropic", "label": "Anthropic"},
                        {"value": "gemini", "label": "Google Gemini"}]),
            _f("base_url", "API base URL", help="For OpenAI-compatible services, the address that ends before "
                                                 "/chat/completions.", placeholder="https://api.openai.com/v1"),
            _f("model", "Model name", required=True, placeholder="gpt-4o-mini"),
            _f("api_key", "API key", "password", secret=True, help="Stored encrypted; never shown again."),
            _f("temperature", "Temperature", "number", help="0 gives the most predictable answers.", placeholder="0.2"),
        ]},
    "smtp": {
        "label": "Email (SMTP)", "group": "Messages",
        "description": "Send scheduled reports and notifications from your own mail server.",
        "fields": [
            _f("host", "SMTP server", required=True, placeholder="smtp.gmail.com"),
            _f("port", "Port", "number", required=True, placeholder="587"),
            _f("tls", "Use STARTTLS", "toggle"),
            _f("username", "User name"),
            _f("password", "Password", "password", secret=True),
            _f("from_email", "From address", required=True, placeholder="hr@yourcompany.in"),
            _f("from_name", "From name", placeholder="HR team"),
        ]},
    "sms": {
        "label": "SMS", "group": "Messages",
        "description": "Text messages for sign-in codes and alerts through an Indian SMS gateway (DLT registered).",
        "fields": [
            _f("provider", "Gateway", "select", required=True,
               options=[{"value": v, "label": v} for v in ("MSG91", "Gupshup", "Kaleyra", "Twilio", "Other")]),
            _f("sender_id", "Sender ID (DLT header)", required=True, placeholder="NGHRMS"),
            _f("entity_id", "DLT entity ID"),
            _f("api_key", "API key", "password", secret=True),
        ]},
    "whatsapp": {
        "label": "WhatsApp", "group": "Messages",
        "description": "WhatsApp Business API for payslips, leave balance and approvals on WhatsApp.",
        "fields": [
            _f("provider", "Provider", "select",
               options=[{"value": v, "label": v} for v in ("Meta Cloud API", "Gupshup", "Interakt", "Other")]),
            _f("phone_number", "Business phone number", required=True, placeholder="+91 98xxxxxxx"),
            _f("phone_number_id", "Phone number ID"),
            _f("access_token", "Access token", "password", secret=True),
        ]},
    "accounting": {
        "label": "Accounting (Tally, Zoho Books)", "group": "Finance",
        "description": "How the payroll journal is exported for your accounts.",
        "fields": [
            _f("system", "Accounting software", "select", required=True,
               options=[{"value": "tally", "label": "Tally Prime / ERP 9"}, {"value": "zoho_books", "label": "Zoho Books"},
                        {"value": "quickbooks", "label": "QuickBooks"}, {"value": "csv", "label": "Other (CSV journal)"}]),
            _f("voucher_type", "Voucher type", placeholder="Journal"),
            _f("salary_payable_ledger", "Salary payable ledger", placeholder="Salary Payable"),
            _f("salary_expense_ledger", "Salary expense ledger", placeholder="Salaries and Wages"),
            _f("pf_payable_ledger", "PF payable ledger", placeholder="PF Payable"),
            _f("esi_payable_ledger", "ESI payable ledger", placeholder="ESI Payable"),
            _f("tds_payable_ledger", "TDS payable ledger", placeholder="TDS on Salary Payable"),
            _f("pt_payable_ledger", "PT payable ledger", placeholder="Professional Tax Payable"),
            _f("cost_centre_split", "Split by cost centre", "toggle"),
            _f("zoho_org_id", "Zoho Books organisation ID", help="Only for Zoho Books"),
            _f("zoho_token", "Zoho Books token", "password", secret=True, help="Only for Zoho Books"),
        ]},
    # Google and Microsoft sign-in live in Settings → Sign-in options (modules/sso), not here.
    "job_boards": {
        "label": "Job boards", "group": "Hiring",
        "description": "Post openings to Naukri, LinkedIn and Indeed from the hiring module.",
        "requires": "recruitment",
        "fields": [
            _f("naukri_client_id", "Naukri client ID"),
            _f("naukri_key", "Naukri API key", "password", secret=True),
            _f("linkedin_company_id", "LinkedIn company page ID"),
            _f("indeed_publisher_id", "Indeed publisher ID"),
        ]},
}


def _key() -> bytes:
    secret = (get_settings().SECRET_KEY or "dev").encode()
    return base64.urlsafe_b64encode(hashlib.sha256(b"integrations-v1:" + secret).digest())


def encrypt(data: dict) -> str:
    return Fernet(_key()).encrypt(json.dumps(data).encode()).decode()


def decrypt(token: str | None) -> dict:
    if not token:
        return {}
    try:
        return json.loads(Fernet(_key()).decrypt(token.encode()).decode())
    except Exception:
        return {}


def _available(kind: str) -> bool:
    req = KINDS[kind].get("requires")
    if req == "recruitment":
        try:
            from core.db import Base
            return "jobs" in Base.metadata.tables
        except Exception:
            return False
    return True


def row(db: Session, company_id: int, kind: str) -> IntegrationSetting | None:
    return db.query(IntegrationSetting).filter(IntegrationSetting.company_id == company_id,
                                               IntegrationSetting.kind == kind).first()


def get_config(db: Session, company_id: int, kind: str) -> dict | None:
    """The connection's settings with secrets, or None when it is off."""
    r = row(db, company_id, kind)
    if r is None or not r.enabled:
        return None
    return {**(r.config or {}), **decrypt(r.secrets_encrypted)}


def smtp_config(db: Session, company_id: int) -> dict | None:
    c = get_config(db, company_id, "smtp")
    if not c or not c.get("host") or not c.get("from_email"):
        return None
    return {"host": c["host"], "port": int(c.get("port") or 587), "tls": c.get("tls", True) is not False,
            "username": c.get("username"), "password": c.get("password"), "from_email": c["from_email"],
            "from_name": c.get("from_name")}


def out(kind: str, r: IntegrationSetting | None) -> dict:
    spec = KINDS[kind]
    status = r.status if r else "not_set_up"
    if r and not r.enabled and status != "not_set_up":
        status = "off"
    return {"kind": kind, "label": spec["label"], "group": spec["group"], "description": spec["description"],
            "fields": spec["fields"], "enabled": bool(r and r.enabled), "status": status,
            "config": (r.config if r else {}) or {}, "secret_hints": (r.secret_hints if r else {}) or {},
            "last_tested_at": r.last_tested_at if r else None, "last_message": r.last_message if r else None,
            "updated_by_name": r.updated_by_name if r else None, "updated_at": r.updated_at if r else None}


def list_all(db: Session, company_id: int) -> list[dict]:
    rows = {r.kind: r for r in db.query(IntegrationSetting).filter(IntegrationSetting.company_id == company_id).all()}
    return [out(k, rows.get(k)) for k in KINDS if _available(k)]


def save(db: Session, company_id: int, kind: str, data: dict, actor: str) -> IntegrationSetting:
    if kind not in KINDS or not _available(kind):
        raise BadRequest("Unknown integration.")
    spec = KINDS[kind]
    r = row(db, company_id, kind)
    if r is None:
        r = IntegrationSetting(company_id=company_id, kind=kind)
        db.add(r)
    cfg = dict(r.config or {})
    secrets = decrypt(r.secrets_encrypted)
    hints = dict(r.secret_hints or {})
    incoming = data.get("config") or {}
    for f in spec["fields"]:
        k = f["key"]
        if k not in incoming:
            continue
        v = incoming[k]
        if isinstance(v, str):
            v = v.strip()
        if f["secret"]:
            if v in (None, ""):
                continue                         # blank keeps the stored secret
            secrets[k] = v
            hints[k] = "…" + str(v)[-4:]
        elif f["type"] == "number" and v not in (None, ""):
            try:
                cfg[k] = float(v) if "." in str(v) else int(v)
            except ValueError:
                raise BadRequest(f"{f['label']} should be a number.")
        else:
            cfg[k] = v
    for k in data.get("clear_secrets") or []:
        secrets.pop(k, None)
        hints.pop(k, None)
    enabled = bool(data.get("enabled", r.enabled if r.id else True))
    if enabled:
        missing = [f["label"] for f in spec["fields"] if f["required"] and not (cfg.get(f["key"]) or secrets.get(f["key"]))]
        if missing:
            raise BadRequest("Fill in: " + ", ".join(missing) + ".")
    if kind == "ai_model":
        prov = cfg.get("provider")
        if prov == "openai" and not cfg.get("base_url"):
            cfg["base_url"] = "https://api.openai.com/v1"
        if cfg.get("base_url") and not str(cfg["base_url"]).startswith(("http://", "https://")):
            raise BadRequest("The base URL should start with https://")
        if enabled and prov in ("anthropic", "gemini") and not secrets.get("api_key"):
            raise BadRequest("Enter the API key.")
    r.config, r.secret_hints = cfg, hints
    r.secrets_encrypted = encrypt(secrets) if secrets else None
    r.enabled = enabled
    r.status = "saved" if enabled else "off"
    r.updated_by_name = actor
    flag_modified(r, "config")
    flag_modified(r, "secret_hints")
    db.flush()
    audit.event(db, "integration.saved", "integration_settings", r.id,
                {"kind": kind, "enabled": enabled, "fields": sorted(incoming.keys())}, entity_label=spec["label"])
    return r


def test(db: Session, company_id: int, kind: str, actor_email: str | None = None) -> IntegrationSetting:
    r = row(db, company_id, kind)
    if r is None:
        raise BadRequest("Save the settings first.")
    cfg = {**(r.config or {}), **decrypt(r.secrets_encrypted)}
    ok, msg = False, ""
    try:
        if kind == "ai_model":
            from modules.assistant import providers
            reply = providers.complete_text(cfg, "Reply with the single word: ready")
            ok, msg = True, f"The model answered: {reply[:80]}"
        elif kind == "smtp":
            import smtplib
            port = int(cfg.get("port") or 587)
            smtp = smtplib.SMTP_SSL(cfg["host"], port, timeout=15) if port == 465 else smtplib.SMTP(cfg["host"], port,
                                                                                                    timeout=15)
            with smtp:
                if port != 465 and cfg.get("tls", True):
                    smtp.starttls()
                if cfg.get("username"):
                    smtp.login(cfg["username"], cfg.get("password") or "")
            ok, msg = True, "Signed in to the mail server."
        else:
            ok, msg = True, "Settings saved. The live connection is checked the first time it is used."
    except Exception as exc:
        ok, msg = False, str(getattr(exc, "detail", None) or exc)[:280]
    r.status = "connected" if ok else "error"
    r.last_message = msg
    r.last_tested_at = datetime.now(timezone.utc)
    db.flush()
    return r
