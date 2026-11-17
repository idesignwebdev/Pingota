from __future__ import annotations

import json
import logging
from urllib.parse import quote

from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.billing import mpesa, paystack
from apps.billing.models import Payment, PlanPayment, WebhookEvent
from apps.billing.service import (
    amount_matches,
    fail_payment,
    settle_payment,
    settle_plan_payment,
)
from apps.tenants.models import Isp

log = logging.getLogger(__name__)


def _as_dict(value) -> dict:
    """Paystack usually returns metadata as an object, but can return a string."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _apply_verification(payment: Payment, verified: dict) -> str:
    """Turn a verify response into a settlement decision.

    Only a successful, matching payment provisions access. A mismatch is left
    pending for a human: the money may have left the customer even though the
    amount or currency is wrong.
    """
    status = str(verified.get("status") or "").lower()
    if status in {"failed", "abandoned", "reversed"}:
        fail_payment(payment, str(verified.get("gateway_response") or status))
        return "failed"
    if status != "success":
        return "pending"
    if not paystack.matches_payment(payment, verified):
        log.error(
            "paystack verification mismatch for %s: expected %s %s, got %s %s (reference %s)",
            payment.reference,
            payment.amount,
            payment.currency,
            verified.get("amount"),
            verified.get("currency"),
            verified.get("reference"),
        )
        return "mismatch"

    settle_payment(payment, provider_reference=str(verified.get("id", "")), raw=verified)
    return "paid"


@csrf_exempt
@require_POST
def paystack_webhook(request, isp_slug: str | None = None):
    """Paystack delivery. Signature is checked against the ISP's own keys.

    The tenant is resolved from the slug in the URL, or from event metadata
    for a single shared endpoint.
    """
    raw = request.body
    try:
        payload = json.loads(raw or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"status": "invalid json"}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"status": "invalid json"}, status=400)

    event = payload.get("event", "")
    data = payload.get("data")
    data = data if isinstance(data, dict) else {}
    metadata = _as_dict(data.get("metadata"))

    if isp_slug:
        isp = get_object_or_404(Isp, slug=isp_slug)
    else:
        isp = Isp.objects.filter(slug=metadata.get("isp", "")).first()
        if isp is None:
            return JsonResponse({"status": "ignored", "reason": "unknown ISP"}, status=202)

    if not paystack.valid_signature(raw, request.headers, isp):
        return JsonResponse({"status": "invalid signature"}, status=401)

    WebhookEvent.objects.create(
        provider="paystack", event_id=str(data.get("id") or data.get("reference") or ""), payload=payload
    )

    if event != "charge.success":
        return JsonResponse({"status": "ignored"})

    payment = Payment.objects.filter(reference=data.get("reference"), isp=isp).first()
    if not payment:
        return JsonResponse({"status": "unknown reference"}, status=202)

    # Re-verify server-side; a webhook alone is not proof of payment.
    try:
        verified = paystack.verify(payment.reference, isp)
    except paystack.PaystackError as exc:
        log.warning("paystack verify failed: %s", exc)
        return JsonResponse({"status": "verify failed"}, status=502)

    outcome = _apply_verification(payment, verified)
    # A mismatch is not retryable; 200 stops Paystack resending it forever.
    return JsonResponse({"status": outcome})


@csrf_exempt
@require_POST
def mpesa_callback(request, isp_slug: str):
    isp = get_object_or_404(Isp, slug=isp_slug)
    try:
        payload = json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"ResultCode": 1, "ResultDesc": "Invalid request"})
    payload = payload if isinstance(payload, dict) else {}
    parsed = mpesa.parse_callback(payload)
    WebhookEvent.objects.create(
        provider="mpesa",
        event_id=parsed["checkout_request_id"] or timezone.now().isoformat(),
        payload=payload,
    )

    payment = Payment.objects.filter(
        isp=isp, provider="mpesa", provider_reference=parsed["checkout_request_id"]
    ).first()
    if not payment:
        return JsonResponse({"ResultCode": 0, "ResultDesc": "Accepted"})

    if str(parsed["result_code"]) != "0":
        fail_payment(payment, parsed.get("description", "cancelled"))
        return JsonResponse({"ResultCode": 0, "ResultDesc": "Accepted"})

    if not amount_matches(payment, parsed.get("amount")):
        log.error(
            "mpesa callback amount mismatch for %s: expected %s, got %s",
            payment.reference,
            payment.amount,
            parsed.get("amount"),
        )
        # Do not fail the payment: the customer may have paid a different
        # amount, and an operator needs to reconcile it by hand.
        return JsonResponse({"ResultCode": 0, "ResultDesc": "Accepted"})

    settle_payment(payment, provider_reference=parsed["receipt"], raw=parsed)
    return JsonResponse({"ResultCode": 0, "ResultDesc": "Accepted"})


def checkout_return(request):
    """Where Paystack sends the browser after checkout closes.

    Webhooks are the primary confirmation, but the customer's browser can get
    here first. Verifying here closes the gap instead of leaving a paid guest
    staring at a pending screen until the webhook retries.
    """
    reference = request.GET.get("reference", "")
    payment = Payment.objects.filter(reference=reference).first()
    if not payment:
        return render(
            request,
            "portal/error.html",
            {"message": "We could not find that payment. Return to the capture page and try again."},
            status=404,
        )

    if payment.status == "pending":
        try:
            verified = paystack.verify(payment.reference, payment.isp)
        except paystack.PaystackError as exc:
            # The webhook is still the backstop; do not show a payment error
            # to a guest whose money may well have gone through.
            log.warning("paystack callback verify failed for %s: %s", payment.reference, exc)
        else:
            _apply_verification(payment, verified)

    return redirect(f"/portal/{payment.isp.slug}/status/{payment.reference}/")


def _apply_plan_verification(payment: PlanPayment, verified: dict) -> str:
    """Hosting-plan equivalent of ``_apply_verification``."""
    status = str(verified.get("status") or "").lower()
    if status in {"failed", "abandoned", "reversed"}:
        fail_payment(payment, str(verified.get("gateway_response") or status))
        return "failed"
    if status != "success":
        return "pending"
    if not paystack.matches_payment(payment, verified):
        log.error(
            "plan paystack verification mismatch for %s: expected %s %s, got %s %s (reference %s)",
            payment.reference,
            payment.amount,
            payment.currency,
            verified.get("amount"),
            verified.get("currency"),
            verified.get("reference"),
        )
        return "mismatch"

    settle_plan_payment(payment, provider_reference=str(verified.get("id", "")), raw=verified)
    return "paid"


@csrf_exempt
@require_POST
def plan_paystack_webhook(request):
    """Paystack delivery for a hosting-plan payment.

    Only the platform account can sign these events, so a tenant key can never
    mark another network's plan as paid.
    """
    raw = request.body
    if not paystack.valid_platform_signature(raw, request.headers):
        return JsonResponse({"status": "invalid signature"}, status=401)
    try:
        payload = json.loads(raw or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"status": "invalid json"}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"status": "invalid json"}, status=400)

    event = payload.get("event", "")
    data = payload.get("data")
    data = data if isinstance(data, dict) else {}
    WebhookEvent.objects.create(
        provider="paystack",
        event_id=str(data.get("id") or data.get("reference") or ""),
        payload=payload,
    )

    if event != "charge.success":
        return JsonResponse({"status": "ignored"})

    payment = PlanPayment.objects.filter(reference=data.get("reference")).first()
    if not payment:
        return JsonResponse({"status": "unknown reference"}, status=202)

    try:
        verified = paystack.verify_platform(payment.reference)
    except paystack.PaystackError as exc:
        log.warning("plan paystack verify failed: %s", exc)
        return JsonResponse({"status": "verify failed"}, status=502)

    return JsonResponse({"status": _apply_plan_verification(payment, verified)})


@csrf_exempt
@require_POST
def plan_mpesa_callback(request, reference: str):
    """Safaricom's callback for a platform plan payment.

    No signature scheme is available, so the unguessable reference in the URL
    and the CheckoutRequestID stored from the STK response are the checks, plus
    the amount.
    """
    try:
        payload = json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"ResultCode": 1, "ResultDesc": "Invalid request"})
    payload = payload if isinstance(payload, dict) else {}
    parsed = mpesa.parse_callback(payload)
    WebhookEvent.objects.create(
        provider="mpesa",
        event_id=parsed["checkout_request_id"] or timezone.now().isoformat(),
        payload=payload,
    )

    payment = PlanPayment.objects.filter(reference=reference, provider="mpesa").first()
    if not payment:
        return JsonResponse({"ResultCode": 0, "ResultDesc": "Accepted"})
    if not payment.provider_reference or parsed["checkout_request_id"] != payment.provider_reference:
        log.warning("plan mpesa callback did not match the stored checkout for %s", reference)
        return JsonResponse({"ResultCode": 0, "ResultDesc": "Accepted"})

    if str(parsed["result_code"]) != "0":
        fail_payment(payment, parsed.get("description", "cancelled"))
        return JsonResponse({"ResultCode": 0, "ResultDesc": "Accepted"})

    if not amount_matches(payment, parsed.get("amount")):
        log.error(
            "plan mpesa callback amount mismatch for %s: expected %s, got %s",
            payment.reference,
            payment.amount,
            parsed.get("amount"),
        )
        return JsonResponse({"ResultCode": 0, "ResultDesc": "Accepted"})

    settle_plan_payment(payment, provider_reference=parsed["receipt"], raw=parsed)
    return JsonResponse({"ResultCode": 0, "ResultDesc": "Accepted"})


def plan_checkout_return(request):
    """Where Paystack sends the browser after a hosting-plan checkout."""
    reference = request.GET.get("reference", "")
    payment = PlanPayment.objects.filter(reference=reference).first()
    if not payment:
        return render(request, "admin/plan_error.html", status=404)

    if payment.status == "pending":
        try:
            verified = paystack.verify_platform(payment.reference)
        except paystack.PaystackError as exc:
            log.warning("plan paystack callback verify failed for %s: %s", payment.reference, exc)
        else:
            _apply_plan_verification(payment, verified)

    status_url = reverse("panel:plan_status", args=[payment.reference])
    if not request.user.is_authenticated:
        return redirect(f"/admin/login/?next={quote(status_url)}")
    return redirect(status_url)
