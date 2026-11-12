"""Captive portal.

The portal is plain server-rendered HTML: a customer on a 5-minute-old phone
should never wait for a JS framework to boot before they can pay. The only
JavaScript on the page is the status poller and the MAC capture.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.billing import mpesa, paystack
from apps.billing.models import Payment
from apps.billing.service import mac_from_request
from apps.network.models import AccessGrant, Package
from apps.tenants.models import Isp

log = logging.getLogger(__name__)


def _isp(slug: str | None) -> Isp:
    if slug:
        return get_object_or_404(Isp, slug=slug, status="active")
    isp = Isp.objects.filter(slug=settings.PORTAL_SLUG, status="active").first()
    if isp is None and Isp.objects.filter(status="active").count() == 1:
        # /portal/ is a convenience entry point for single-network installs.
        isp = Isp.objects.filter(status="active").first()
    if isp is None:
        raise Http404("No matching portal")
    return isp


def _resolve(request, isp_slug=None) -> Isp:
    isp = _isp(isp_slug)
    request.isp = isp
    return isp


def _live_grant(isp: Isp, mac: str):
    if not mac:
        return None
    return (
        AccessGrant.objects.filter(
            isp=isp, customer__mac_address=mac, state="active", expires_at__gt=timezone.now()
        )
        .select_related("package")
        .first()
    )


def index(request, isp_slug: str | None = None):
    isp = _resolve(request, isp_slug)
    mac = mac_from_request(request)
    if mac:
        request.session["portal_mac"] = mac
    live = _live_grant(isp, mac)
    return render(
        request,
        "portal/index.html",
        {
            "isp": isp,
            "mac": mac,
            "live": live,
            "packages_qs": isp.packages.filter(is_active=True),
            "hotspot": {
                "link_login_only": request.GET.get("link-login-only", ""),
                "link_orig": request.GET.get("link-orig", ""),
            },
        },
    )


def packages(request, isp_slug: str):
    isp = _resolve(request, isp_slug)
    mac = mac_from_request(request)
    selected = None
    if request.method == "POST":
        # No-JS path: the pass form posts here, then we redirect to checkout.
        selected = isp.packages.filter(pk=request.POST.get("package_id"), is_active=True).first()
        if selected:
            request.session["pending_phone"] = (request.POST.get("phone") or "")[:24]
            request.session["pending_email"] = (request.POST.get("email") or "")[:120]
            request.session["pending_provider"] = request.POST.get("provider") or "paystack"
            # Stay in this POST so the no-JS form can create the payment
            # without a redirect to a POST-only URL.
            return checkout(request, isp.slug, selected.id)
    return render(
        request,
        "portal/packages.html",
        {
            "isp": isp,
            "mac": mac,
            "packages_qs": isp.packages.filter(is_active=True),
            "selected_package": selected,
        },
    )


@require_POST
def checkout(request, isp_slug: str, package_id):
    """Create the payment and send the customer to Paystack (or STK prompt)."""
    isp = _resolve(request, isp_slug)
    # The no-JS form posts to a fixed checkout URL; read the chosen pass from
    # the body first so a radio change works without JavaScript.
    selected_id = request.POST.get("package_id") or package_id
    try:
        package = Package.objects.filter(pk=selected_id, isp=isp, is_active=True).first()
    except (ValidationError, ValueError, TypeError):
        package = None
    if package is None:
        return render(
            request,
            "portal/error.html",
            {"isp": isp, "message": "That pass is no longer available. Choose another pass and try again."},
            status=400,
        )
    mac = mac_from_request(request)
    if not mac:
        return render(
            request,
            "portal/error.html",
            {"isp": isp, "message": "We could not read this device's network address. Reconnect to the hotspot and try again."},
            status=400,
        )
    request.session["portal_mac"] = mac

    session = request.session
    phone = (request.POST.get("phone") or session.get("pending_phone") or "").strip()
    email = (
        request.POST.get("email") or session.get("pending_email") or ""
    ).strip() or f"guest@{isp.slug}.local"
    provider = request.POST.get("provider") or session.get("pending_provider") or "paystack"
    if provider == "mpesa" and not phone:
        return render(
            request,
            "portal/error.html",
            {"isp": isp, "message": "Enter the M-Pesa number to receive the payment prompt."},
            status=400,
        )

    payment = Payment.objects.create(
        isp=isp,
        package=package,
        provider=provider,
        reference=paystack.new_reference(),
        amount=Decimal(package.price),
        currency=isp.currency,
        phone=phone,
        status="pending",
    )
    # Attach the customer now so webhooks do not depend on session state.
    from apps.network.models import Customer

    customer, _ = Customer.objects.get_or_create(isp=isp, mac_address=mac, defaults={"phone": phone})
    payment.customer = customer
    payment.save(update_fields=["customer"])

    if provider == "mpesa":
        try:
            body = mpesa.stk_push(payment, phone)
            payment.provider_reference = body.get("CheckoutRequestID", "")
            payment.raw_response = body
            payment.save(update_fields=["provider_reference", "raw_response"])
            return redirect("portal:status", isp_slug=isp.slug, reference=payment.reference)
        except (mpesa.MpesaError, ValueError) as exc:
            log.warning("mpesa stk push failed: %s", exc)
            payment.status = "failed"
            payment.failure_reason = str(exc)[:200]
            payment.save(update_fields=["status", "failure_reason"])
            return render(
                request,
                "portal/error.html",
                {
                    "isp": isp,
                    "message": "The M-Pesa prompt could not be sent. Check the phone number and try again, or pay by card.",
                },
                status=502,
            )

    try:
        data = paystack.initialize(
            payment,
            email=email,
            callback_url=f"{settings.PUBLIC_BASE_URL}/payments/callback/",
            metadata={"mac": mac},
        )
        payment.checkout_url = data.get("authorization_url", "")
        payment.raw_response = data
        payment.save(update_fields=["checkout_url", "raw_response"])
        return redirect(payment.checkout_url)
    except paystack.PaystackError as exc:
        log.warning("paystack init failed: %s", exc)
        payment.status = "failed"
        payment.failure_reason = str(exc)[:200]
        payment.save(update_fields=["status", "failure_reason"])
        return render(
            request,
            "portal/error.html",
            {
                "isp": isp,
                "message": "Card payment is unavailable right now. Try M-Pesa, or come back in a few minutes.",
            },
            status=502,
        )


@require_POST
def redeem_voucher(request, isp_slug: str):
    """A counter code instead of a payment. Same provisioning path."""
    from apps.network.vouchers import redeem, VoucherError

    isp = _resolve(request, isp_slug)
    mac = mac_from_request(request)
    if not mac:
        return render(
            request,
            "portal/error.html",
            {
                "isp": isp,
                "message": "We could not read this device's address. Reconnect to the Wi-Fi and enter the code again.",
            },
            status=400,
        )
    request.session["portal_mac"] = mac
    try:
        voucher, grant = redeem(isp, request.POST.get("voucher", ""), mac)
    except VoucherError as exc:
        return render(
            request,
            "portal/error.html",
            {"isp": isp, "message": str(exc)},
            status=400,
        )
    return render(
        request,
        "portal/voucher_ok.html",
        {"isp": isp, "mac": mac, "grant": grant, "voucher": voucher},
    )


def status(request, isp_slug: str, reference: str):
    isp = _resolve(request, isp_slug)
    payment = get_object_or_404(Payment, reference=reference, isp=isp)
    # Prefer the grant created by this payment so an expired pass shows as
    # ended instead of looking like it is still activating.
    grant = payment.grant or (
        _live_grant(isp, payment.customer.mac_address) if payment.customer else None
    )
    return render(
        request,
        "portal/status.html",
        {"isp": isp, "payment": payment, "grant": grant},
    )


@require_GET
def status_state(request, isp_slug: str, reference: str):
    isp = _resolve(request, isp_slug)
    payment = get_object_or_404(Payment, reference=reference, isp=isp)
    grant = payment.grant or (
        _live_grant(isp, payment.customer.mac_address) if payment.customer else None
    )
    return JsonResponse(
        {
            "payment": {"status": payment.status, "reference": payment.reference},
            "grant": (
                {
                    "state": grant.state,
                    "expires_at": grant.expires_at.isoformat(),
                    "seconds_left": grant.seconds_left,
                    "package": grant.package.name,
                }
                if grant
                else None
            ),
            "connect_url": request.session.get("hotspot_link_login_only", ""),
        }
    )


@require_GET
def my_pass(request, isp_slug: str):
    isp = _resolve(request, isp_slug)
    mac = mac_from_request(request)
    grant = _live_grant(isp, mac)
    recent = (
        AccessGrant.objects.filter(isp=isp, customer__mac_address=mac).select_related("package")[:10]
        if mac
        else []
    )
    return render(request, "portal/pass.html", {"isp": isp, "mac": mac, "grant": grant, "recent": recent})
