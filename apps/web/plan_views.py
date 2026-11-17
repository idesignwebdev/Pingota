"""Hosting-plan checkout for a signed-in operator.

Kept apart from the pricing page and the tenant panel guard: a network whose
trial or plan has ended must still be able to reach these pages and pay.
"""
from __future__ import annotations

import logging
from functools import wraps

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET

from apps.billing import mpesa, paystack
from apps.billing.models import PlanPayment
from apps.billing.service import fail_payment
from apps.tenants.models import SubscriptionPlan
from core.security import enforce_request_limit

log = logging.getLogger(__name__)


def billing_required(view):
    """Membership is required, but an ended trial is not a barrier here."""

    @wraps(view)
    @login_required
    def wrapper(request, *args, **kwargs):
        membership = getattr(request.user, "membership", None)
        request.membership = membership
        if membership is None:
            if request.user.is_superuser:
                return redirect("/django-admin/")
            raise Http404("This account is not attached to a network.")
        request.isp = membership.isp
        return view(request, *args, **kwargs)

    return wrapper


def purchasable_plans():
    """Every plan row the platform owner has priced."""
    return SubscriptionPlan.objects.all().order_by("monthly_price")


def _checkout_context(request, plan, *, error="", field_errors=None):
    return {
        "isp": request.isp,
        "plan": plan,
        "plan_currency": settings.PAYMENT_DEFAULT_CURRENCY,
        "paystack_enabled": paystack.platform_configured(),
        "mpesa_enabled": mpesa.platform_configured(),
        "email": getattr(request.user, "email", "")
        or (request.user.get_username() if "@" in request.user.get_username() else ""),
        "phone": request.isp.phone,
        "error": error,
        "field_errors": field_errors or {},
    }


def _clean_phone(raw: str) -> str:
    phone = (raw or "").replace("+", "").replace(" ", "").replace("-", "")
    if phone.startswith("0"):
        phone = "254" + phone[1:]
    if not phone.startswith("254") or len(phone) != 12 or not phone.isdigit():
        raise ValueError("Enter the M-Pesa number, for example 0712 345 678.")
    return phone


@billing_required
def checkout(request, plan_code: str):
    """Show the plan and start a payment with the chosen rail."""
    plan = get_object_or_404(SubscriptionPlan, code=plan_code)
    isp = request.isp

    if request.method == "POST":
        blocked = enforce_request_limit(
            request,
            "plan_checkout",
            10,
            60,
            retry_url=request.path,
            retry_label="Back to checkout",
        )
        if blocked is not None:
            return blocked

        context = _checkout_context(request, plan)
        if isp.manually_suspended:
            context["error"] = (
                "Pingota has paused this network. Contact support before making a payment."
            )
            return render(request, "admin/plan_checkout.html", context, status=403)

        provider = (request.POST.get("provider") or "").strip().lower()
        if provider not in {"paystack", "mpesa"}:
            context["error"] = "Choose a payment method."
            return render(request, "admin/plan_checkout.html", context, status=400)
        if provider == "paystack" and not paystack.platform_configured():
            context["error"] = "Card payment is not available right now. Try M-Pesa."
            return render(request, "admin/plan_checkout.html", context, status=400)
        if provider == "mpesa" and not mpesa.platform_configured():
            context["error"] = "M-Pesa is not available right now. Try card payment."
            return render(request, "admin/plan_checkout.html", context, status=400)

        email = ""
        phone = ""
        field_errors = {}
        if provider == "paystack":
            email = (request.POST.get("email") or "").strip()[:120]
            try:
                validate_email(email)
            except ValidationError:
                field_errors["email"] = "Enter the email address for the payment receipt."
        else:
            try:
                phone = _clean_phone(request.POST.get("phone") or "")
            except ValueError as exc:
                field_errors["phone"] = str(exc)

        if field_errors:
            context.update({"email": email, "phone": phone, "field_errors": field_errors})
            return render(request, "admin/plan_checkout.html", context, status=400)

        payment = PlanPayment.objects.create(
            isp=isp,
            plan=plan,
            provider=provider,
            reference=paystack.new_reference("plan"),
            amount=plan.monthly_price,
            currency=settings.PAYMENT_DEFAULT_CURRENCY,
            phone=phone,
            status="pending",
        )

        if provider == "mpesa":
            try:
                body = mpesa.stk_push_platform(payment, phone)
                checkout_id = body.get("CheckoutRequestID", "")
                if not checkout_id:
                    raise mpesa.MpesaError("M-Pesa did not return a checkout number")
                payment.provider_reference = checkout_id
                payment.raw_response = body
                payment.save(update_fields=["provider_reference", "raw_response"])
                return redirect("panel:plan_status", reference=payment.reference)
            except (mpesa.MpesaError, ValueError) as exc:
                log.warning("plan mpesa stk push failed: %s", exc)
                fail_payment(payment, str(exc))
                context["error"] = (
                    "The M-Pesa prompt could not be sent. Check the phone number and try again."
                )
                return render(request, "admin/plan_checkout.html", context, status=502)

        try:
            data = paystack.initialize_platform(
                payment,
                email=email,
                callback_url=f"{settings.PUBLIC_BASE_URL}/payments/plan/callback/",
                metadata={"plan": plan.code},
            )
            payment.checkout_url = data.get("authorization_url", "")
            payment.raw_response = data
            payment.save(update_fields=["checkout_url", "raw_response"])
            return redirect(payment.checkout_url)
        except paystack.PaystackError as exc:
            log.warning("plan paystack initialize failed: %s", exc)
            fail_payment(payment, str(exc))
            context["error"] = (
                "Card payment is unavailable right now. Try M-Pesa, or come back in a few minutes."
            )
            return render(request, "admin/plan_checkout.html", context, status=502)

    return render(request, "admin/plan_checkout.html", _checkout_context(request, plan))


@billing_required
def status(request, reference: str):
    payment = get_object_or_404(
        PlanPayment.objects.select_related("plan"),
        reference=reference,
        isp=request.isp,
    )
    return render(
        request,
        "admin/plan_status.html",
        {
            "isp": request.isp,
            "payment": payment,
            "plan": payment.plan,
            "plan_currency": settings.PAYMENT_DEFAULT_CURRENCY,
        },
    )


@billing_required
@require_GET
def status_state(request, reference: str):
    payment = get_object_or_404(
        PlanPayment.objects.select_related("plan"),
        reference=reference,
        isp=request.isp,
    )
    return JsonResponse(
        {
            "status": payment.status,
            "plan": payment.plan.name,
            "plan_ends_at": (
                payment.period_end.isoformat() if payment.period_end else None
            ),
            "status_url": reverse("panel:plan_status", args=[payment.reference]),
        }
    )
