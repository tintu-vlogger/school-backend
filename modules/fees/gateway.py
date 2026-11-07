"""Online fee payments: provider settings (secrets encrypted), checkout orders, verification
and webhooks.

Providers
- test      simulated payments: the flow works end to end without keys (no real money moves)
- razorpay  Orders API + Checkout; payment verified with the HMAC signature, webhook
            `payment.captured` / `order.paid` (X-Razorpay-Signature)
- stripe    Checkout Session (redirect); verified by reading the session back, webhook
            `checkout.session.completed` (Stripe-Signature)
- cashfree  PG Orders API (payment_session_id for the Cashfree JS checkout); verified by reading the
            order back, webhook signed with the secret key
- payu      hosted checkout form with a SHA-512 hash; PayU posts back to our return URL, where the
            reverse hash is checked

When test mode is on (default), or the chosen provider has no keys yet, checkout uses the test
provider. Secrets are encrypted at rest with a key derived from the app secret and are never
returned; the API returns only the last four characters as a hint.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal

from cryptography.fernet import Fernet
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from core import audit, context
from core.config import get_settings
from core.errors import BadRequest, NotFound
from modules.fees import service, settings as fee_settings
from modules.fees.models import FeeGatewaySetting, FeeInvoice, FeeOnlineOrder

logger = logging.getLogger("hrms")


def _f(key, label, secret=False, required=True, help=""):
    return {"key": key, "label": label, "secret": secret, "required": required, "help": help}


PROVIDERS = {
    "test": {"label": "Test mode", "description": "Simulated payments for trying the flow. No money moves.",
             "fields": []},
    "razorpay": {"label": "Razorpay", "description": "Cards, UPI, net banking and wallets (India).",
                 "fields": [_f("key_id", "Key ID"), _f("key_secret", "Key secret", True),
                            _f("webhook_secret", "Webhook secret", True, False,
                               "Set the same secret on the Razorpay webhook.")]},
    "stripe": {"label": "Stripe", "description": "Cards and local payment methods worldwide.",
               "fields": [_f("publishable_key", "Publishable key", required=False), _f("secret_key", "Secret key", True),
                          _f("webhook_secret", "Webhook signing secret", True, False)]},
    "payu": {"label": "PayU", "description": "Hosted checkout (India).",
             "fields": [_f("merchant_key", "Merchant key"), _f("merchant_salt", "Merchant salt", True)]},
    "cashfree": {"label": "Cashfree", "description": "UPI, cards and net banking (India).",
                 "fields": [_f("app_id", "App ID"), _f("secret_key", "Secret key", True)]},
}


def _key() -> bytes:
    secret = (get_settings().SECRET_KEY or "dev").encode()
    return base64.urlsafe_b64encode(hashlib.sha256(b"fees-gateway-v1:" + secret).digest())


def encrypt(data: dict) -> str:
    return Fernet(_key()).encrypt(json.dumps(data).encode()).decode()


def decrypt(token: str | None) -> dict:
    if not token:
        return {}
    try:
        return json.loads(Fernet(_key()).decrypt(token.encode()).decode())
    except Exception:
        return {}


def row(db: Session, company_id: int) -> FeeGatewaySetting | None:
    return db.query(FeeGatewaySetting).filter(FeeGatewaySetting.company_id == company_id).first()


def _secrets_all(r: FeeGatewaySetting | None) -> dict:
    """Secrets are kept per provider: {"razorpay": {...}, "stripe": {...}}."""
    return decrypt(r.secrets_encrypted) if r else {}


def _has_keys(provider: str, cfg: dict, secrets: dict) -> bool:
    spec = PROVIDERS.get(provider)
    if not spec or provider == "test":
        return False
    return all((cfg.get(f["key"]) or secrets.get(f["key"])) for f in spec["fields"] if f["required"])


def out(db: Session, company_id: int) -> dict:
    r = row(db, company_id)
    provider = r.provider if r else "test"
    cfg_all = (r.config if r else {}) or {}
    hints_all = (r.secret_hints if r else {}) or {}
    sec_all = _secrets_all(r)
    return {
        "provider": provider, "enabled": bool(r.enabled) if r else True, "test_mode": bool(r.test_mode) if r else True,
        "providers": [{"key": k, "label": v["label"], "description": v["description"], "fields": v["fields"],
                       "config": cfg_all.get(k, {}), "secret_hints": hints_all.get(k, {}),
                       "ready": k == "test" or _has_keys(k, cfg_all.get(k, {}), sec_all.get(k, {}))}
                      for k, v in PROVIDERS.items()],
        "live": effective(db, company_id)[0] != "test",
        "updated_by_name": r.updated_by_name if r else None, "updated_at": r.updated_at if r else None,
    }


def save(db: Session, company_id: int, data: dict) -> dict:
    provider = data.get("provider") or "test"
    if provider not in PROVIDERS:
        raise BadRequest("Unknown payment provider.")
    r = row(db, company_id)
    if r is None:
        r = FeeGatewaySetting(company_id=company_id)
        db.add(r)
    cfg_all = dict(r.config or {})
    hints_all = dict(r.secret_hints or {})
    sec_all = _secrets_all(r)
    incoming = data.get("config") or {}
    cfg, hints, sec = dict(cfg_all.get(provider, {})), dict(hints_all.get(provider, {})), dict(sec_all.get(provider, {}))
    for f in PROVIDERS[provider]["fields"]:
        k = f["key"]
        if k not in incoming:
            continue
        v = incoming[k]
        v = v.strip() if isinstance(v, str) else v
        if f["secret"]:
            if v in (None, ""):
                continue  # blank keeps the stored secret
            sec[k] = v
            hints[k] = "…" + str(v)[-4:]
        else:
            cfg[k] = v
    if "sandbox" in incoming:
        cfg["sandbox"] = bool(incoming.get("sandbox"))
    for k in data.get("clear_secrets") or []:
        sec.pop(k, None)
        hints.pop(k, None)
    cfg_all[provider], hints_all[provider], sec_all[provider] = cfg, hints, sec
    test_mode = bool(data.get("test_mode", r.test_mode if r.id else True))
    if provider != "test" and not test_mode and not _has_keys(provider, cfg, sec):
        missing = [f["label"] for f in PROVIDERS[provider]["fields"] if f["required"] and not (cfg.get(f["key"]) or sec.get(f["key"]))]
        raise BadRequest("Fill in: " + ", ".join(missing) + ", or keep test mode on.")
    r.provider = provider
    r.enabled = bool(data.get("enabled", True))
    r.test_mode = test_mode or provider == "test"
    r.config, r.secret_hints = cfg_all, hints_all
    r.secrets_encrypted = encrypt(sec_all) if any(sec_all.values()) else None
    r.updated_by_name = context.current().actor_name
    flag_modified(r, "config")
    flag_modified(r, "secret_hints")
    db.flush()
    audit.event(db, "fees.gateway_saved", "fee_gateway_settings", r.id,
                {"provider": provider, "test_mode": r.test_mode, "enabled": r.enabled, "fields": sorted(incoming.keys())},
                company_id=company_id, entity_label=PROVIDERS[provider]["label"])
    return out(db, company_id)


def effective(db: Session, company_id: int) -> tuple[str, dict]:
    """(provider actually used for checkout, its config with secrets)."""
    r = row(db, company_id)
    if r is None or not r.enabled:
        return ("test", {}) if r is None else ("off", {})
    cfg = dict((r.config or {}).get(r.provider, {}))
    sec = _secrets_all(r).get(r.provider, {})
    if r.test_mode or r.provider == "test" or not _has_keys(r.provider, cfg, sec):
        return "test", {}
    return r.provider, {**cfg, **sec}


def provider_config(db: Session, company_id: int, provider: str) -> dict:
    r = row(db, company_id)
    if r is None:
        return {}
    return {**((r.config or {}).get(provider, {})), **_secrets_all(r).get(provider, {})}


# ── Orders ───────────────────────────────────────────────────────────

def _minor(amount: Decimal, currency: str) -> int:
    zero_decimal = {"JPY", "KRW", "VND", "CLP", "ISK", "UGX", "XAF", "XOF"}
    return int(amount) if currency.upper() in zero_decimal else int((amount * 100).to_integral_value())


def create_order(db: Session, company_id: int, student, invoice_uuids: list[str], *, portal_account_id: int | None,
                 return_url: str | None, api_base: str | None) -> dict:
    cfg = fee_settings.get(db, company_id)
    if not cfg.get("online_payments", True):
        raise BadRequest("Online payment is switched off. Please pay at the fee counter.")
    provider, pcfg = effective(db, company_id)
    if provider == "off":
        raise BadRequest("Online payment is not set up yet. Please pay at the fee counter.")
    invs = service.open_invoices(db, company_id, student.id)
    if not invs:
        raise BadRequest("There is nothing to pay.")
    chosen = [i for i in invs if str(i.uuid) in set(invoice_uuids or [])] if invoice_uuids else invs
    if not chosen:
        raise BadRequest("Pick the instalments to pay.")
    if not cfg.get("allow_partial_online", True) and len(chosen) != len(invs):
        raise BadRequest("Please pay all dues together.")
    # Pay in order: an instalment cannot be skipped while an older one is unpaid.
    ids = {i.id for i in chosen}
    oldest_unpicked = next((i for i in invs if i.id not in ids), None)
    if oldest_unpicked and any(i.due_date > oldest_unpicked.due_date for i in chosen):
        raise BadRequest(f"Please pay {oldest_unpicked.title} first.")
    on = service.today(db, company_id)
    amount = sum((service.q(i.balance) + service.late_fee_due(db, i, on) for i in chosen), service.D0)
    if amount <= 0:
        raise BadRequest("There is nothing to pay.")
    currency = fee_settings.currency(db, company_id)
    order = FeeOnlineOrder(company_id=company_id, student_id=student.id, portal_account_id=portal_account_id,
                           invoice_ids=[i.id for i in chosen], amount=amount, currency=currency, provider=provider,
                           test_mode=provider == "test")
    db.add(order)
    db.flush()
    from modules.company.models import Company
    company = db.query(Company).filter(Company.id == company_id).first()
    result = {"order": str(order.uuid), "provider": provider, "test_mode": order.test_mode, "amount": amount,
              "currency": currency, "description": ", ".join(i.title for i in chosen)[:200],
              "institution": company.company_name if company else ""}
    back = _with_order(return_url, order) if return_url else None
    try:
        if provider == "test":
            order.provider_order_id = f"test_{order.uuid.hex[:16]}"
        elif provider == "razorpay":
            data = _razorpay_order(pcfg, order, company)
            order.provider_order_id = data["id"]
            result["checkout"] = {"key": pcfg["key_id"], "order_id": data["id"], "amount": data["amount"],
                                  "currency": data["currency"], "name": result["institution"],
                                  "description": result["description"],
                                  "prefill": {"name": student.full_name, "email": student.email or ""}}
        elif provider == "stripe":
            data = _stripe_session(pcfg, order, result["description"], back)
            order.provider_order_id = data["id"]
            result["redirect_url"] = data["url"]
        elif provider == "cashfree":
            data = _cashfree_order(pcfg, order, student, back)
            order.provider_order_id = data.get("order_id") or order.uuid.hex
            result["checkout"] = {"payment_session_id": data.get("payment_session_id"),
                                  "mode": "sandbox" if pcfg.get("sandbox") else "production"}
        elif provider == "payu":
            order.provider_order_id = order.uuid.hex[:24]
            result["form"] = _payu_form(db, pcfg, order, student, company, result["description"], api_base, back)
    except BadRequest:
        raise
    except Exception as exc:  # network or provider error: tell the parent plainly
        logger.warning("Online payment order failed for company %s: %s", company_id, exc)
        order.status = "failed"
        order.error = str(exc)[:500]
        db.flush()
        raise BadRequest("The payment provider could not be reached. Please try again in a few minutes.")
    db.flush()
    audit.event(db, "fees.online_order_created", "fee_online_orders", order.id,
                {"provider": provider, "amount": str(amount), "invoices": order.invoice_ids},
                company_id=company_id, entity_uuid=str(order.uuid))
    return result


def _with_order(url: str, order: FeeOnlineOrder) -> str:
    if not url.startswith(("http://", "https://")):
        raise BadRequest("Invalid return address.")
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}order={order.uuid}"


def _razorpay_order(cfg: dict, order: FeeOnlineOrder, company) -> dict:
    import httpx
    r = httpx.post("https://api.razorpay.com/v1/orders", auth=(cfg["key_id"], cfg["key_secret"]), timeout=20,
                   json={"amount": _minor(Decimal(order.amount), order.currency), "currency": order.currency,
                         "receipt": order.uuid.hex[:40], "notes": {"order": str(order.uuid)}})
    if r.status_code >= 400:
        raise RuntimeError(f"Razorpay {r.status_code}: {r.text[:200]}")
    return r.json()


def _stripe_session(cfg: dict, order: FeeOnlineOrder, description: str, back: str | None) -> dict:
    import httpx
    if not back:
        raise BadRequest("Missing return address.")
    form = {
        "mode": "payment", "client_reference_id": str(order.uuid), "metadata[order]": str(order.uuid),
        "line_items[0][quantity]": "1", "line_items[0][price_data][currency]": order.currency.lower(),
        "line_items[0][price_data][unit_amount]": str(_minor(Decimal(order.amount), order.currency)),
        "line_items[0][price_data][product_data][name]": description or "Fees",
        "success_url": back, "cancel_url": back + "&cancelled=1",
    }
    r = httpx.post("https://api.stripe.com/v1/checkout/sessions", data=form, auth=(cfg["secret_key"], ""), timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"Stripe {r.status_code}: {r.text[:200]}")
    return r.json()


def _cashfree_base(cfg: dict) -> str:
    return "https://sandbox.cashfree.com/pg" if cfg.get("sandbox") else "https://api.cashfree.com/pg"


def _cashfree_headers(cfg: dict) -> dict:
    return {"x-client-id": cfg["app_id"], "x-client-secret": cfg["secret_key"], "x-api-version": "2023-08-01"}


def _cashfree_order(cfg: dict, order: FeeOnlineOrder, student, back: str | None) -> dict:
    import httpx
    body = {"order_id": order.uuid.hex, "order_amount": float(order.amount), "order_currency": order.currency,
            "customer_details": {"customer_id": f"s{student.id}", "customer_name": student.full_name,
                                 "customer_phone": (student.phone or "9999999999")[-10:],
                                 "customer_email": student.email or None},
            "order_meta": {"return_url": back} if back else {}}
    r = httpx.post(f"{_cashfree_base(cfg)}/orders", json=body, headers=_cashfree_headers(cfg), timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"Cashfree {r.status_code}: {r.text[:200]}")
    return r.json()


def _payu_hash(cfg: dict, f: dict) -> str:
    seq = "|".join([cfg["merchant_key"], f["txnid"], f["amount"], f["productinfo"], f["firstname"], f["email"],
                    f.get("udf1", ""), f.get("udf2", ""), "", "", "", "", "", "", "", "", cfg["merchant_salt"]])
    return hashlib.sha512(seq.encode()).hexdigest()


def _payu_form(db: Session, cfg: dict, order: FeeOnlineOrder, student, company, description: str,
               api_base: str | None, back: str | None) -> dict:
    slug = company.slug if company else ""
    ret = f"{(api_base or '').rstrip('/')}/api/v1/public/fees/return/payu/{slug}"
    fields = {"key": cfg["merchant_key"], "txnid": order.provider_order_id, "amount": f"{Decimal(order.amount):.2f}",
              "productinfo": (description or "Fees")[:100], "firstname": student.first_name,
              "email": student.email or "parent@example.com", "phone": (student.phone or "9999999999")[-10:],
              "udf1": str(order.uuid), "udf2": (back or "")[:250], "surl": ret, "furl": ret}
    fields["hash"] = _payu_hash(cfg, fields)
    action = "https://test.payu.in/_payment" if cfg.get("sandbox") else "https://secure.payu.in/_payment"
    return {"action": action, "fields": fields}


def get_order(db: Session, company_id: int, uuid) -> FeeOnlineOrder:
    o = db.query(FeeOnlineOrder).filter(FeeOnlineOrder.company_id == company_id, FeeOnlineOrder.uuid == uuid).first()
    if o is None:
        raise NotFound("Payment")
    return o


def mark_paid(db: Session, order: FeeOnlineOrder, provider_payment_id: str | None) -> FeeOnlineOrder:
    """Record the money and issue the receipt (idempotent: a second call does nothing)."""
    if order.status == "paid":
        return order
    from modules.students.models import Student
    cid = order.company_id
    student = db.query(Student).filter(Student.company_id == cid, Student.id == order.student_id).first()
    still_open = [i.id for i in service.open_invoices(db, cid, student.id, order.invoice_ids)]
    label = "Test payment" if order.test_mode else f"{PROVIDERS.get(order.provider, {}).get('label', order.provider)}"
    pay = service.collect(db, cid, student, modes=[{"mode": "online", "amount": order.amount,
                                                    "reference": provider_payment_id or order.provider_order_id,
                                                    "bank": label}],
                          invoice_ids=still_open, source="online", online_order_id=order.id,
                          collected_by_name="Online payment", notes=f"Paid online ({label})")
    order.status = "paid"
    order.provider_payment_id = provider_payment_id
    order.payment_id = pay.id
    order.paid_at = datetime.now(timezone.utc)
    db.flush()
    try:
        from modules.notifications.service import notify_portal
        from modules.students.service import portal_account_ids_for_students
        ids = portal_account_ids_for_students(db, cid, [student.id])
        if ids:
            notify_portal(db, cid, ids, kind="fees.paid", title=f"Payment received: {order.currency} {service.q(order.amount)}",
                          body=f"Receipt {pay.number} for {student.full_name}.", link="/dashboard/my/fees")
    except Exception as exc:  # a notification never undoes a payment
        logger.warning("Fee payment notification failed: %s", exc)
    return order


def verify(db: Session, order: FeeOnlineOrder, data: dict) -> FeeOnlineOrder:
    """Confirm a payment the browser reports (never trusted without checking with the provider)."""
    if order.status == "paid":
        return order
    cfg = provider_config(db, order.company_id, order.provider)
    if order.provider == "razorpay":
        oid, pid, sig = data.get("razorpay_order_id"), data.get("razorpay_payment_id"), data.get("razorpay_signature")
        if oid != order.provider_order_id or not pid or not sig:
            raise BadRequest("The payment could not be confirmed.")
        expected = hmac.new(cfg.get("key_secret", "").encode(), f"{oid}|{pid}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            raise BadRequest("The payment could not be confirmed.")
        return mark_paid(db, order, pid)
    if order.provider == "stripe":
        import httpx
        r = httpx.get(f"https://api.stripe.com/v1/checkout/sessions/{order.provider_order_id}",
                      auth=(cfg.get("secret_key", ""), ""), timeout=20)
        body = r.json() if r.status_code < 400 else {}
        if body.get("payment_status") == "paid":
            return mark_paid(db, order, body.get("payment_intent"))
        raise BadRequest("The payment has not been completed yet.")
    if order.provider == "cashfree":
        import httpx
        r = httpx.get(f"{_cashfree_base(cfg)}/orders/{order.provider_order_id}", headers=_cashfree_headers(cfg), timeout=20)
        body = r.json() if r.status_code < 400 else {}
        if body.get("order_status") == "PAID":
            return mark_paid(db, order, body.get("cf_order_id") and str(body.get("cf_order_id")))
        raise BadRequest("The payment has not been completed yet.")
    if order.provider == "payu":
        if order.status != "paid":
            raise BadRequest("The payment has not been completed yet.")
        return order
    raise BadRequest("Use the test payment button for test orders.")


def test_pay(db: Session, order: FeeOnlineOrder, succeed: bool = True) -> FeeOnlineOrder:
    if not order.test_mode:
        raise BadRequest("This is not a test payment.")
    if order.status == "paid":
        return order
    if not succeed:
        order.status = "failed"
        order.error = "Cancelled in test mode"
        db.flush()
        return order
    return mark_paid(db, order, f"testpay_{order.uuid.hex[:12]}")


# ── Webhooks (no sign-in; the signature proves the sender) ──────────

def _find_order(db: Session, company_id: int, provider: str, provider_order_id: str | None) -> FeeOnlineOrder | None:
    if not provider_order_id:
        return None
    return db.query(FeeOnlineOrder).filter(FeeOnlineOrder.company_id == company_id, FeeOnlineOrder.provider == provider,
                                           FeeOnlineOrder.provider_order_id == provider_order_id).first()


def webhook(db: Session, company_id: int, provider: str, body: bytes, headers: dict) -> dict:
    cfg = provider_config(db, company_id, provider)
    if provider == "razorpay":
        secret = cfg.get("webhook_secret") or ""
        sig = headers.get("x-razorpay-signature") or ""
        if not secret or not hmac.compare_digest(hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(), sig):
            raise BadRequest("Invalid signature")
        ev = json.loads(body or b"{}")
        if ev.get("event") in ("payment.captured", "order.paid"):
            pay = ((ev.get("payload") or {}).get("payment") or {}).get("entity") or {}
            order = _find_order(db, company_id, "razorpay", pay.get("order_id"))
            if order:
                mark_paid(db, order, pay.get("id"))
                return {"ok": True, "order": str(order.uuid)}
        return {"ok": True}
    if provider == "stripe":
        secret = cfg.get("webhook_secret") or ""
        parts = dict(p.split("=", 1) for p in (headers.get("stripe-signature") or "").split(",") if "=" in p)
        signed = f"{parts.get('t', '')}.".encode() + body
        if not secret or not hmac.compare_digest(hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest(),
                                                 parts.get("v1", "")):
            raise BadRequest("Invalid signature")
        ev = json.loads(body or b"{}")
        obj = (ev.get("data") or {}).get("object") or {}
        if ev.get("type") == "checkout.session.completed" and obj.get("payment_status") == "paid":
            order = _find_order(db, company_id, "stripe", obj.get("id"))
            if order:
                mark_paid(db, order, obj.get("payment_intent"))
                return {"ok": True, "order": str(order.uuid)}
        return {"ok": True}
    if provider == "cashfree":
        secret = cfg.get("secret_key") or ""
        ts = headers.get("x-webhook-timestamp") or ""
        sig = headers.get("x-webhook-signature") or ""
        expected = base64.b64encode(hmac.new(secret.encode(), ts.encode() + body, hashlib.sha256).digest()).decode()
        if not secret or not hmac.compare_digest(expected, sig):
            raise BadRequest("Invalid signature")
        ev = json.loads(body or b"{}")
        data = ev.get("data") or {}
        if (data.get("payment") or {}).get("payment_status") == "SUCCESS":
            order = _find_order(db, company_id, "cashfree", (data.get("order") or {}).get("order_id"))
            if order:
                mark_paid(db, order, str((data.get("payment") or {}).get("cf_payment_id") or ""))
                return {"ok": True, "order": str(order.uuid)}
        return {"ok": True}
    if provider == "payu":
        from urllib.parse import parse_qs
        form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
        order = payu_return(db, company_id, form)
        return {"ok": True, "order": str(order.uuid) if order else None}
    raise NotFound("Provider")


def payu_return(db: Session, company_id: int, form: dict) -> FeeOnlineOrder | None:
    cfg = provider_config(db, company_id, "payu")
    if not cfg.get("merchant_salt"):
        raise BadRequest("PayU is not set up.")
    seq = "|".join([cfg["merchant_salt"], form.get("status", ""), "", "", "", "", "", "", "", "", "",
                    form.get("udf2", ""), form.get("udf1", ""), form.get("email", ""), form.get("firstname", ""),
                    form.get("productinfo", ""), form.get("amount", ""), form.get("txnid", ""), cfg.get("merchant_key", "")])
    if not hmac.compare_digest(hashlib.sha512(seq.encode()).hexdigest(), form.get("hash", "")):
        raise BadRequest("Invalid signature")
    order = _find_order(db, company_id, "payu", form.get("txnid"))
    if order and form.get("status") == "success":
        mark_paid(db, order, form.get("mihpayid"))
    elif order and order.status != "paid":
        order.status = "failed"
        order.error = form.get("error_Message") or form.get("status")
        db.flush()
    return order
