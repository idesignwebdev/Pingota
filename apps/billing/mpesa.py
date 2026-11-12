"""Safaricom Daraja (STK Push) for ISPs that want settlements in their own till.

Each ISP supplies their own Consumer Key/Secret, shortcode and passkey, so
money never passes through the platform. The platform only receives the
callback and flips the payment — that keeps this a software product rather than
a payments intermediary.
"""
from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal

import httpx
from django.conf import settings

log = logging.getLogger(__name__)

SANDBOX = "https://sandbox.safaricom.co.ke"
PRODUCTION = "https://api.safaricom.co.ke"
TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class MpesaError(RuntimeError):
    pass


def _base_url(env: str) -> str:
    return SANDBOX if (env or "").lower() == "sandbox" else PRODUCTION


def access_token(cfg) -> str:
    auth = base64.b64encode(f"{cfg.get_consumer_key()}:{cfg.get_secret()}".encode()).decode()
    resp = httpx.get(
        f"{_base_url(settings.MPESA_ENV)}/oauth/v1/generate",
        params={"grant_type": "client_credentials"},
        headers={"Authorization": f"Basic {auth}"},
        timeout=TIMEOUT,
    )
    if resp.status_code >= 400:
        raise MpesaError(f"Daraja auth failed: HTTP {resp.status_code}")
    return resp.json().get("access_token", "")


def stk_push(payment, phone: str) -> dict:
    cfg = getattr(payment.isp, "mpesa", None)
    if not cfg or not cfg.enabled:
        raise MpesaError("M-Pesa is not configured for this ISP")

    phone = phone.replace("+", "").replace(" ", "")
    if phone.startswith("0"):
        phone = "254" + phone[1:]
    if not phone.startswith("254") or len(phone) != 12:
        raise MpesaError("Phone number must be in the form 2547XXXXXXXX")

    shortcode = cfg.get_shortcode()
    if not shortcode:
        raise MpesaError("M-Pesa shortcode is not configured")

    timestamp = datetime.now(dt_timezone.utc).strftime("%Y%m%d%H%M%S")
    password = base64.b64encode(f"{shortcode}{cfg.get_passkey()}{timestamp}".encode()).decode()

    payload = {
        "BusinessShortCode": shortcode,
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": int(Decimal(payment.amount)),
        "PartyA": phone,
        "PartyB": shortcode,
        "PhoneNumber": phone,
        "CallBackURL": f"{settings.PUBLIC_BASE_URL}/payments/mpesa/callback/{payment.isp.slug}/",
        "AccountReference": payment.reference[:12],
        "TransactionDesc": f"{payment.isp.name} {payment.package.name}"[:60],
    }
    resp = httpx.post(
        f"{_base_url(settings.MPESA_ENV)}/mpesa/stkpush/v1/processrequest",
        json=payload,
        headers={"Authorization": f"Bearer {access_token(cfg)}"},
        timeout=TIMEOUT,
    )
    body = resp.json() if resp.content else {}
    if resp.status_code >= 400 or body.get("ResponseCode") not in ("0", 0):
        raise MpesaError(body.get("errorMessage") or "STK push was rejected")
    return body


def parse_callback(payload: dict) -> dict:
    """Flatten Safaricom's callback into the fields we store."""
    stk = (payload or {}).get("Body", {}).get("stkCallback", {})
    items = (stk.get("CallbackMetadata") or {}).get("Item") or []
    meta = {item.get("Name"): item.get("Value") for item in items}
    return {
        "result_code": stk.get("ResultCode"),
        "checkout_request_id": stk.get("CheckoutRequestID", ""),
        "merchant_request_id": stk.get("MerchantRequestID", ""),
        "receipt": meta.get("MpesaReceiptNumber", ""),
        "amount": meta.get("Amount"),
        "phone": str(meta.get("PhoneNumber", "")),
        "description": stk.get("ResultDesc", ""),
    }
