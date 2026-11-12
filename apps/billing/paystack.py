"""Paystack integration.

Flow: create a pending Payment -> POST /transaction/initialize -> redirect the
customer to ``authorization_url`` -> Paystack calls our webhook -> we verify the
signature, re-confirm the amount with GET /transaction/verify/:ref, then
provision. Settlement: the ISP's own secret key + subaccount when configured,
otherwise the platform account from the environment.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from decimal import Decimal

import httpx
from django.conf import settings

log = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class PaystackError(RuntimeError):
    pass


def _secret_for(isp) -> str:
    """Per-ISP key first, then the optional platform-wide key."""
    cfg = getattr(isp, "paystack", None)
    if cfg and cfg.enabled and cfg.get_secret():
        return cfg.get_secret()
    return settings.PAYSTACK_SECRET_KEY


def _subaccount_for(isp) -> str:
    cfg = getattr(isp, "paystack", None)
    return cfg.subaccount_code if cfg and cfg.enabled else ""


def new_reference(prefix: str = "pg") -> str:
    return f"{prefix}-{secrets.token_hex(8)}"


def initialize(payment, *, email: str, callback_url: str, metadata: dict | None = None) -> dict:
    """Ask Paystack for a checkout URL. Amount is sent in the minor unit."""
    secret = _secret_for(payment.isp)
    if not secret:
        raise PaystackError("Paystack is not configured for this ISP")

    payload = {
        "email": email,
        "amount": int(Decimal(payment.amount) * 100),
        "currency": payment.currency,
        "reference": payment.reference,
        "callback_url": callback_url,
        "metadata": {"isp": payment.isp.slug, "package": payment.package.name, **(metadata or {})},
    }
    subaccount = _subaccount_for(payment.isp)
    if subaccount:
        payload["subaccount"] = subaccount

    resp = httpx.post(
        f"{settings.PAYSTACK_BASE_URL}/transaction/initialize",
        json=payload,
        headers={"Authorization": f"Bearer {secret}"},
        timeout=TIMEOUT,
    )
    body = resp.json() if resp.content else {}
    if resp.status_code >= 400 or not body.get("status"):
        raise PaystackError(body.get("message") or f"Paystack returned HTTP {resp.status_code}")
    return body["data"]


def verify(reference: str, isp) -> dict:
    secret = _secret_for(isp)
    resp = httpx.get(
        f"{settings.PAYSTACK_BASE_URL}/transaction/verify/{reference}",
        headers={"Authorization": f"Bearer {secret}"},
        timeout=TIMEOUT,
    )
    body = resp.json() if resp.content else {}
    if resp.status_code >= 400 or not body.get("status"):
        raise PaystackError(body.get("message") or "Verification failed")
    return body["data"]


def valid_signature(raw_body: bytes, signature: str, isp) -> bool:
    """HMAC-SHA512 of the raw body with the secret key, per Paystack docs."""
    secret = _secret_for(isp)
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, signature)
