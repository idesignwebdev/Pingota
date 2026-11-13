"""Paystack integration.

Flow: create a pending Payment -> POST /transaction/initialize -> redirect the
customer to ``authorization_url`` -> Paystack calls our webhook -> we verify the
signature, re-confirm the amount with GET /transaction/verify/:ref, then
provision. Settlement: the ISP's own secret key + subaccount when configured,
otherwise the platform account from the environment.

Two signature schemes are accepted. Paystack's current scheme signs
``<timestamp>.<raw body>`` with the webhook signing secret (``whsec_...``) and
sends ``Paystack-Signature``; the legacy scheme signs the raw body with the
account secret key and sends ``x-paystack-signature``. Both are checked so an
account can move to a signing secret without dropping deliveries.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from decimal import Decimal

import httpx
from django.conf import settings

log = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(10.0, connect=5.0)
SIGNATURE_TOLERANCE_SECONDS = 300


class PaystackError(RuntimeError):
    pass


def _secret_for(isp) -> str:
    """Per-ISP key first, then the optional platform-wide key."""
    cfg = getattr(isp, "paystack", None)
    if cfg and cfg.enabled and cfg.get_secret():
        return cfg.get_secret()
    return settings.PAYSTACK_SECRET_KEY


def _subaccount_for(isp) -> str:
    """A subaccount only belongs to the account that created it.

    When the ISP supplies its own secret key, the money already settles to that
    account, so sending a subaccount code from another account would be wrong.
    """
    cfg = getattr(isp, "paystack", None)
    if not cfg or not cfg.enabled or cfg.get_secret():
        return ""
    return cfg.subaccount_code


def _webhook_secrets_for(isp) -> list[str]:
    """Signing secrets that may have signed an event for this ISP."""
    secrets_found = []
    cfg = getattr(isp, "paystack", None)
    if cfg:
        secret = cfg.get_webhook_secret()
        if secret:
            secrets_found.append(secret)
    if settings.PAYSTACK_WEBHOOK_SECRET:
        secrets_found.append(settings.PAYSTACK_WEBHOOK_SECRET)
    return list(dict.fromkeys(secrets_found))


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


def _parse_signature_header(header: str) -> tuple[int | None, list[str]]:
    """Read ``t=...,v1=<key id>:<hex>[,v1=...]`` into a timestamp and hashes."""
    timestamp = None
    signatures = []
    for part in (header or "").split(","):
        key, separator, value = part.strip().partition("=")
        if not separator:
            continue
        if key == "t":
            if value.isdigit():
                timestamp = int(value)
        elif key == "v1":
            _key_id, separator, digest = value.partition(":")
            candidate = digest if separator else value
            if len(candidate) == 128 and all(char in "0123456789abcdef" for char in candidate):
                signatures.append(candidate)
    return timestamp, signatures


def _valid_new_signature(raw_body: bytes, header: str, signing_secrets: list[str]) -> bool:
    timestamp, signatures = _parse_signature_header(header)
    if timestamp is None or not signatures:
        return False
    if abs(int(time.time()) - timestamp) > SIGNATURE_TOLERANCE_SECONDS:
        return False

    message = str(timestamp).encode() + b"." + raw_body
    for secret in signing_secrets:
        expected = hmac.new(secret.encode(), message, hashlib.sha512).hexdigest()
        for signature in signatures:
            if hmac.compare_digest(expected, signature):
                return True
    return False


def valid_signature(raw_body: bytes, headers, isp) -> bool:
    """True when either Paystack signature scheme validates this delivery."""
    signing_secrets = _webhook_secrets_for(isp)
    new_header = headers.get("paystack-signature") or headers.get("Paystack-Signature") or ""
    if new_header and _valid_new_signature(raw_body, new_header, signing_secrets):
        return True

    legacy_signature = headers.get("x-paystack-signature") or ""
    secret = _secret_for(isp)
    if legacy_signature and secret:
        expected = hmac.new(secret.encode(), raw_body, hashlib.sha512).hexdigest()
        if hmac.compare_digest(expected, legacy_signature):
            return True
    return False


def matches_payment(payment, verified: dict) -> bool:
    """Check the verified amount, currency and reference against the intent.

    Paystack returns the amount in the minor unit. A verify call that does not
    match the pass that was priced must never provision access.
    """
    try:
        amount_minor = int(verified.get("amount"))
        expected_minor = int(Decimal(str(payment.amount)) * 100)
    except (ArithmeticError, TypeError, ValueError):
        return False
    if amount_minor != expected_minor:
        return False

    currency = str(verified.get("currency") or "").upper()
    if currency and currency != str(payment.currency).upper():
        return False

    reference = str(verified.get("reference") or "")
    return not reference or reference == payment.reference
