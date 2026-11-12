from __future__ import annotations

import json
import logging

from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.billing import mpesa, paystack
from apps.billing.models import Payment, WebhookEvent
from apps.billing.service import fail_payment, settle_payment
from apps.tenants.models import Isp

log = logging.getLogger(__name__)


@csrf_exempt
@require_POST
def paystack_webhook(request, isp_slug: str | None = None):
    """Paystack delivery. Signature is checked against the ISP's own key.

    The tenant is resolved from the slug in the URL, or from event metadata
    for a single shared endpoint.
    """
    raw = request.body
    payload = json.loads(raw or b"{}")
    event = payload.get("event", "")
    data = payload.get("data", {}) or {}

    if isp_slug:
        isp = get_object_or_404(Isp, slug=isp_slug)
    else:
        slug = (data.get("metadata") or {}).get("isp", "")
        isp = Isp.objects.filter(slug=slug).first()
        if isp is None:
            return JsonResponse({"status": "ignored", "reason": "unknown ISP"}, status=202)

    if not paystack.valid_signature(raw, request.headers.get("x-paystack-signature", ""), isp):
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

    if verified.get("status") != "success":
        fail_payment(payment, verified.get("gateway_response", "not successful"))
        return JsonResponse({"status": "not successful"})

    settle_payment(payment, provider_reference=str(verified.get("id", "")), raw=verified)
    return JsonResponse({"status": "ok"})


@csrf_exempt
@require_POST
def mpesa_callback(request, isp_slug: str):
    isp = get_object_or_404(Isp, slug=isp_slug)
    payload = json.loads(request.body or b"{}")
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

    settle_payment(payment, provider_reference=parsed["receipt"], raw=parsed)
    return JsonResponse({"ResultCode": 0, "ResultDesc": "Accepted"})


def checkout_return(request):
    """Where Paystack sends the browser after checkout closes."""
    reference = request.GET.get("reference", "")
    payment = Payment.objects.filter(reference=reference).first()
    if not payment:
        return HttpResponse("Unknown payment reference", status=404)
    return redirect(f"/portal/{payment.isp.slug}/status/{payment.reference}/")
