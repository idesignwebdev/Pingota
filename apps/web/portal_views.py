"""Captive portal.

The portal is plain server-rendered HTML: a customer on a 5-minute-old phone
should never wait for a JS framework to boot before they can pay. The only
JavaScript on the page is the status poller and the MAC capture.
"""
from __future__ import annotations

import logging
import re
import secrets
from decimal import Decimal
from functools import wraps
from urllib.parse import urlencode

from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.billing import mpesa, paystack
from apps.billing.models import Payment
from apps.billing.service import mac_from_request
from apps.network.mikrotik import hotspot_user_name, normalise_mac
from apps.network.models import AccessGrant, Package
from apps.tenants.models import Isp
from core.security import enforce_request_limit

log = logging.getLogger(__name__)


def _isp(slug: str | None) -> Isp:
    if slug:
        # Status is checked by ``portal_access`` so a paused network can
        # explain itself instead of looking like a missing slug.
        return get_object_or_404(Isp, slug=slug)
    isp = Isp.objects.filter(slug=settings.PORTAL_SLUG).first()
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


def _suspended_page(request, isp: Isp):
    return render(request, "portal/suspended.html", {"isp": isp}, status=403)


def portal_access(view):
    """Resolve the tenant once, then stop every guest service when paused."""

    @wraps(view)
    def wrapper(request, isp_slug=None, **kwargs):
        isp = _resolve(request, isp_slug)
        if isp.service_suspended:
            return _suspended_page(request, isp)
        return view(request, isp, **kwargs)

    return wrapper


def portal_access_json(view):
    """Same guard for the poller, which needs JSON rather than an HTML page."""

    @wraps(view)
    def wrapper(request, isp_slug=None, **kwargs):
        isp = _resolve(request, isp_slug)
        if isp.service_suspended:
            return JsonResponse({"status": "paused"}, status=503)
        return view(request, isp, **kwargs)

    return wrapper


def _rails(isp: Isp) -> list[str]:
    """Payment methods a guest can actually complete on this network."""
    rails = []
    if paystack.configured_for(isp):
        rails.append("paystack")
    if mpesa.configured_for(isp):
        rails.append("mpesa")
    return rails


def _known_customer(isp: Isp, mac: str):
    """The device's earlier visit, so the form starts filled in."""
    from apps.network.models import Customer

    if not mac:
        return None
    try:
        mac = normalise_mac(mac)
    except ValueError:
        return None
    return Customer.objects.filter(isp=isp, mac_address=mac).first()


def _live_grant(isp: Isp, mac: str):
    if not mac:
        return None
    return (
        AccessGrant.objects.filter(
            isp=isp, customer__mac_address=mac, state="active", expires_at__gt=timezone.now()
        )
        .exclude(revoked_reason="waiting for router")
        .select_related("package")
        .first()
    )


@portal_access
def index(request, isp):
    mac = mac_from_request(request)
    if mac:
        request.session["portal_mac"] = mac
    # MikroTik hands these in from the hotspot login page; the status poller
    # needs them to sign the device in once the pass exists.
    link_login_only = (request.GET.get("link-login-only") or "")[:200]
    if link_login_only:
        request.session["hotspot_link_login_only"] = link_login_only
        request.session["hotspot_link_orig"] = (request.GET.get("link-orig") or "")[:300]
    live = _live_grant(isp, mac)
    if live and link_login_only:
        # Coming back to the portal with a pass already paid for: finish the
        # router login instead of showing "you are online" on an offline device.
        connect = _hotspot_login_url(request, live)
        if connect:
            return redirect(connect)
    return render(
        request,
        "portal/index.html",
        {
            "isp": isp,
            "mac": mac,
            "live": live,
            "packages_qs": isp.packages.filter(is_active=True),
            "providers": _rails(isp),
            "known_customer": _known_customer(isp, mac),
            "hotspot": {
                "link_login_only": request.GET.get("link-login-only", ""),
                "link_orig": request.GET.get("link-orig", ""),
            },
        },
    )


@portal_access
def packages(request, isp):
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
            return _checkout_impl(request, isp, selected.id)
    return render(
        request,
        "portal/packages.html",
        {
            "isp": isp,
            "mac": mac,
            "packages_qs": isp.packages.filter(is_active=True),
            "providers": _rails(isp),
            "known_customer": _known_customer(isp, mac),
            "selected_package": selected,
        },
    )


@portal_access
@require_POST
def checkout(request, isp, package_id):
    """Create the payment and send the customer to Paystack (or STK prompt)."""
    return _checkout_impl(request, isp, package_id)


def _checkout_impl(request, isp, package_id):
    mac = mac_from_request(request)
    # A busy hotspot NATs every guest behind one public address, so the IP cap
    # is deliberately loose. The per-device cap is what stops one phone (or a
    # script that rotates MACs) from creating payments in a loop.
    blocked = enforce_request_limit(
        request,
        "checkout_ip",
        120,
        60,
        template="portal/rate_limited.html",
        context={"isp": isp},
        retry_url=f"/portal/{isp.slug}/packages/",
        retry_label="Back to passes",
    )
    if blocked is not None:
        return blocked
    if mac:
        blocked = enforce_request_limit(
            request,
            "checkout_mac",
            10,
            60,
            identifier=mac,
            template="portal/rate_limited.html",
            context={"isp": isp},
            retry_url=f"/portal/{isp.slug}/packages/",
            retry_label="Back to passes",
        )
        if blocked is not None:
            return blocked
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
    raw_email = (request.POST.get("email") or session.get("pending_email") or "").strip()
    if raw_email and "@" in raw_email and "." in raw_email.split("@")[-1]:
        email = raw_email
    else:
        clean_num = re.sub(r"[^0-9]", "", phone)
        clean_mac = re.sub(r"[^0-9a-fA-F]", "", mac)
        guest_id = clean_num or clean_mac or secrets.token_hex(4)
        clean_slug = re.sub(r"[^a-zA-Z0-9-]", "", isp.slug) or "guest"
        email = f"guest.{guest_id}@{clean_slug}.pingota.co.ke"
    # Never call a provider the ISP has not configured: that used to end in a
    # 502, which Cloudflare replaced with its own error page.
    rails = _rails(isp)
    requested = (request.POST.get("provider") or "").strip().lower()
    if requested:
        provider = requested
    else:
        provider = session.get("pending_provider") or (rails[0] if rails else "")
    if provider not in rails:
        return render(
            request,
            "portal/error.html",
            {
                "isp": isp,
                "message": (
                    "That payment method is not available on this network. Choose another one, "
                    "or ask the operator for a voucher code."
                    if rails
                    else "Payments are not switched on for this network yet. Ask the operator for a voucher code."
                ),
            },
            status=400,
        )
    if provider == "mpesa" and not phone:
        return render(
            request,
            "portal/error.html",
            {"isp": isp, "message": "Enter the M-Pesa number to receive the payment prompt."},
            status=400,
        )
    if provider == "mpesa":
        # One number must not be used as an STK-prompt target by a script.
        blocked = enforce_request_limit(
            request,
            "mpesa_stk",
            3,
            300,
            identifier=phone,
            template="portal/rate_limited.html",
            context={"isp": isp},
            retry_url=f"/portal/{isp.slug}/packages/",
            retry_label="Back to passes",
        )
        if blocked is not None:
            return blocked

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
                status=400,
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
            status=400,
        )


@portal_access
@require_POST
def redeem_voucher(request, isp):
    """A counter code instead of a payment. Same provisioning path."""
    from apps.network.vouchers import redeem, VoucherError

    mac = mac_from_request(request)
    blocked = enforce_request_limit(
        request,
        "voucher_ip",
        120,
        300,
        template="portal/rate_limited.html",
        context={"isp": isp},
        retry_url=f"/portal/{isp.slug}/packages/",
        retry_label="Back to passes",
    )
    if blocked is not None:
        return blocked
    if mac:
        blocked = enforce_request_limit(
            request,
            "voucher_mac",
            10,
            300,
            identifier=mac,
            template="portal/rate_limited.html",
            context={"isp": isp},
            retry_url=f"/portal/{isp.slug}/packages/",
            retry_label="Back to passes",
        )
        if blocked is not None:
            return blocked
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
    if grant.revoked_reason != "waiting for router":
        # Live on the router already: let the portal bounce the device through
        # the hotspot login instead of leaving it on the login page.
        return redirect("portal:isp_index", isp_slug=isp.slug)
    return render(
        request,
        "portal/voucher_ok.html",
        {"isp": isp, "mac": mac, "grant": grant, "voucher": voucher},
    )


@portal_access
@require_POST
def reconcile(request, isp):
    """Auto-reconcile a transaction reference, M-Pesa receipt, or support code.

    A customer who already paid via M-Pesa or card — or holding a voucher/support
    code — can enter their reference to activate access on their device immediately
    without waiting for webhooks or asking the operator.
    """
    raw_code = (request.POST.get("code") or request.POST.get("reference") or "").strip()
    mac = mac_from_request(request) or request.session.get("portal_mac", "")
    if not raw_code:
        return render(
            request,
            "portal/error.html",
            {"isp": isp, "message": "Enter your transaction ID, payment reference, or voucher/support code."},
            status=400,
        )
    if not mac:
        return render(
            request,
            "portal/error.html",
            {"isp": isp, "message": "We could not read this device's address. Reconnect to the Wi-Fi and try again."},
            status=400,
        )

    blocked = enforce_request_limit(
        request,
        "reconcile_mac",
        10,
        300,
        identifier=mac,
        template="portal/rate_limited.html",
        context={"isp": isp},
        retry_url=f"/portal/{isp.slug}/",
        retry_label="Back to portal",
    )
    if blocked is not None:
        return blocked

    request.session["portal_mac"] = mac
    code = raw_code.strip()
    clean_code = re.sub(r"[^A-Za-z0-9_-]", "", code)

    from django.db.models import Q

    from apps.billing.models import WebhookEvent
    from apps.billing.service import fail_payment, settle_payment
    from apps.billing.views import _apply_verification
    from apps.network.models import Customer
    from apps.network.provisioning import activate_grant, open_grant
    from apps.network.vouchers import VoucherError, redeem

    # 1. Search Payment matching reference, provider_reference (e.g. M-Pesa receipt), or raw response
    payment = (
        Payment.objects.filter(isp=isp)
        .filter(
            Q(reference__iexact=clean_code)
            | Q(provider_reference__iexact=clean_code)
            | Q(raw_response__icontains=clean_code)
        )
        .order_by("-created_at")
        .first()
    )

    if payment:
        if payment.status == "pending":
            if payment.provider == "paystack":
                try:
                    verified = paystack.verify(payment.reference, isp)
                    _apply_verification(payment, verified)
                except Exception as exc:
                    log.warning("reconcile paystack verify failed for %s: %s", payment.reference, exc)
            elif payment.provider == "mpesa":
                # Check recent WebhookEvent for M-Pesa receipt or checkout id
                events = WebhookEvent.objects.filter(provider="mpesa").order_by("-received_at")[:50]
                matched = False
                for ev in events:
                    raw_str = str(ev.payload)
                    if (clean_code.upper() in raw_str.upper()) or (
                        payment.provider_reference and payment.provider_reference in raw_str
                    ):
                        parsed = mpesa.parse_callback(ev.payload)
                        if str(parsed.get("result_code")) == "0":
                            settle_payment(
                                payment,
                                provider_reference=parsed.get("receipt") or clean_code,
                                raw=ev.payload,
                            )
                            matched = True
                            break
                if not matched and isp.mpesa and isp.mpesa.enabled and payment.provider_reference:
                    try:
                        res = mpesa.query_stk_status(payment)
                        if str(res.get("ResultCode")) == "0":
                            settle_payment(payment, provider_reference=clean_code, raw=res)
                        elif res.get("ResultCode") not in (None, "", "1037"):
                            fail_payment(payment, res.get("ResultDesc") or "failed")
                    except Exception as exc:
                        log.warning("reconcile mpesa query failed for %s: %s", payment.reference, exc)

        payment.refresh_from_db()
        if payment.status == "paid":
            customer, _ = Customer.objects.get_or_create(
                isp=isp, mac_address=mac, defaults={"phone": payment.phone}
            )
            grant = payment.grant or _live_grant(isp, mac)
            if not grant or not grant.is_live:
                grant = open_grant(isp, mac=mac, package=payment.package, phone=payment.phone)
                payment.grant = grant
                payment.customer = customer
                payment.save(update_fields=["grant", "customer"])
            elif grant.customer.mac_address != mac:
                grant.customer = customer
                grant.save(update_fields=["customer"])

            try:
                activate_grant(grant)
            except Exception as exc:
                log.warning("reconcile grant activate failed: %s", exc)

            connect = _hotspot_login_url(request, grant)
            if connect:
                return redirect(connect)
            return redirect("portal:status", isp_slug=isp.slug, reference=payment.reference)

        if payment.status == "failed":
            return render(
                request,
                "portal/error.html",
                {
                    "isp": isp,
                    "message": f"Payment {clean_code} failed or was cancelled ({payment.failure_reason or 'declined'}). Try choosing a pass to pay again.",
                },
                status=400,
            )

    # 2. Check if code is a Voucher code
    try:
        voucher, grant = redeem(isp, clean_code, mac)
        connect = _hotspot_login_url(request, grant)
        if connect:
            return redirect(connect)
        return redirect("portal:isp_index", isp_slug=isp.slug)
    except VoucherError:
        pass

    # 3. Check if code matches an AccessGrant support code or ID
    grant = (
        AccessGrant.objects.filter(isp=isp)
        .filter(
            Q(support_code__iexact=clean_code)
            | Q(id__icontains=clean_code.lower())
        )
        .order_by("-started_at")
        .first()
    )
    if grant:
        if grant.is_live:
            if grant.customer.mac_address != mac:
                customer, _ = Customer.objects.get_or_create(isp=isp, mac_address=mac)
                grant.customer = customer
                grant.save(update_fields=["customer"])
            try:
                activate_grant(grant)
            except Exception as exc:
                log.warning("reconcile grant activate error: %s", exc)
            connect = _hotspot_login_url(request, grant)
            if connect:
                return redirect(connect)
            return redirect("portal:my_pass", isp_slug=isp.slug)
        else:
            return render(
                request,
                "portal/error.html",
                {
                    "isp": isp,
                    "message": f"Pass with support code {grant.display_support_code} ended on {grant.expires_at.strftime('%d %b, %H:%M')}. Buy another pass to get online.",
                },
                status=400,
            )

    return render(
        request,
        "portal/error.html",
        {
            "isp": isp,
            "message": (
                f"We could not find a confirmed payment, voucher, or pass matching '{clean_code}'. "
                "Check the transaction code in your M-Pesa SMS or receipt and try again, or ask the operator for help."
            ),
        },
        status=404,
    )


@portal_access
def status(request, isp, reference: str):
    payment = get_object_or_404(Payment, reference=reference, isp=isp)
    # Prefer the grant created by this payment so an expired pass shows as
    # ended instead of looking like it is still activating.
    grant = payment.grant or (
        _live_grant(isp, payment.customer.mac_address) if payment.customer else None
    )
    connect_url = _hotspot_login_url(request, grant) if (grant and grant.is_live) else ""
    if connect_url and not request.GET.get("connected"):
        return redirect(connect_url)

    return render(
        request,
        "portal/status.html",
        {"isp": isp, "payment": payment, "grant": grant, "connect_url": connect_url},
    )


@portal_access_json
@require_GET
def status_state(request, isp, reference: str):
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
            "connect_url": _hotspot_login_url(request, grant),
        }
    )


def _hotspot_login_url(request, grant) -> str:
    """RouterOS login URL that signs the device in once its pass exists.

    The hotspot login page handed us ``link-login-only``; submitting the user
    the panel created turns "paid" into "online" without the guest retyping
    anything. The password is the device MAC, which the router already knows.
    """
    link = request.session.get("hotspot_link_login_only", "")
    mac = request.session.get("portal_mac", "") or (
        grant.customer.mac_address if grant and grant.customer else ""
    )
    if not (
        mac
        and grant
        and grant.state == "active"
        and grant.seconds_left > 0
        and grant.revoked_reason != "waiting for router"
    ):
        return ""
    if not link:
        link = "http://192.168.88.1/login"
    try:
        params = {
            "username": hotspot_user_name(mac),
            "password": normalise_mac(mac).replace(":", ""),
        }
    except ValueError:
        return ""
    original = request.session.get("hotspot_link_orig", "")
    if original:
        params["dst"] = original
    else:
        status_ref = (
            grant.payments.first().reference
            if hasattr(grant, "payments") and grant.payments.exists()
            else ""
        )
        if status_ref:
            params["dst"] = (
                f"{settings.PUBLIC_BASE_URL}/portal/{grant.isp.slug}/status/{status_ref}/?connected=1"
            )
        else:
            params["dst"] = f"{settings.PUBLIC_BASE_URL}/portal/{grant.isp.slug}/?connected=1"
    return f"{link}?{urlencode(params)}"


@require_GET
def hotspot_login(request, isp_slug):
    """The login page the router downloads over ``/tool fetch``.

    MikroTik substitutes ``$(mac)`` and ``$(link-login-only)`` while serving
    this file, so they have to arrive at the router untouched.
    """
    isp = get_object_or_404(Isp, slug=isp_slug)
    portal_url = (
        f"{settings.PUBLIC_BASE_URL}/portal/{isp.slug}/"
        "?mac=$(mac)&link-login-only=$(link-login-only)&link-orig=$(link-orig)"
    )
    response = render(
        request, "portal/hotspot_login.html", {"isp": isp, "portal_url": portal_url}
    )
    response["Cache-Control"] = "no-store"
    return response


@portal_access
@require_GET
def my_pass(request, isp):
    mac = mac_from_request(request)
    grant = _live_grant(isp, mac)
    recent = (
        AccessGrant.objects.filter(isp=isp, customer__mac_address=mac).select_related("package")[:10]
        if mac
        else []
    )
    return render(request, "portal/pass.html", {"isp": isp, "mac": mac, "grant": grant, "recent": recent})
