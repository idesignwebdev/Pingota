"""Operator panel, one tenant per login.

The ISP comes from the signed-in user's membership, never from a URL or a
setting. Every query below filters on it: there is no view in this module that
can read another ISP's payments, routers or customers.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, time, timedelta
from decimal import Decimal
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Sum
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.network import mikrotik
from apps.network.models import AccessGrant, Customer, Package, RouterMetric, SessionEvent
from apps.network.provisioning import revoke_grant

log = logging.getLogger(__name__)


def current_isp(request):
    """The signed-in operator's ISP. Membership is the only source of truth."""
    membership = getattr(request.user, "membership", None)
    if membership is None:
        raise Http404("This account is not attached to a network. Ask the platform owner to invite it.")
    return membership.isp


def panel_required(view):
    """Tenant-panel guard: membership first, then trial state.

    A superuser without a membership is the platform owner, not an ISP
    operator. Send them to Django admin instead of showing the tenant 404.
    """

    @wraps(view)
    @login_required
    def wrapper(request, *args, **kwargs):
        membership = getattr(request.user, "membership", None)
        if membership is None:
            if request.user.is_superuser:
                return redirect("/django-admin/")
            raise Http404("This account is not attached to a network. Ask the platform owner to invite it.")
        if membership.isp.trial_has_ended:
            return render(request, "admin/trial_ended.html", {"isp": membership.isp}, status=403)
        return view(request, *args, **kwargs)

    return wrapper


def platform_only(request):
    """Guard for pages that manage tenants themselves (not ISP data)."""
    if not request.user.is_superuser or getattr(request.user, "membership", None) is not None:
        raise Http404()


def _pct(numerator, denominator) -> int:
    if not denominator:
        return 0
    return int(round(numerator * 100 / denominator))


def _daily_totals(isp, *, days: int, fmt: str) -> list[dict]:
    """Settled revenue for exactly ``days`` local calendar days, ending today.

    Bucketed in Python rather than with TruncDate: MongoDB does not support
    date truncation with a timezone, and the row counts here (a few thousand at
    most) are cheaper to group in the app than to pre-aggregate. Days without a
    sale stay in the list with zero so the chart keeps its shape.
    """
    first_day = timezone.localdate() - timedelta(days=days - 1)
    start = timezone.make_aware(datetime.combine(first_day, time.min))
    totals = {first_day + timedelta(days=index): Decimal("0") for index in range(days)}
    for paid_at, amount in isp.payments.filter(status="paid", paid_at__gte=start).values_list(
        "paid_at", "amount"
    ):
        if not paid_at:
            continue
        day = timezone.localtime(paid_at).date()
        if day in totals:
            totals[day] += amount
    return [
        {"day": day.strftime(fmt), "total": float(total)}
        for day, total in sorted(totals.items())
    ]


@panel_required
def overview(request):
    isp = current_isp(request)
    now = timezone.now()
    day_ago = now - timedelta(hours=24)
    week_ago = now - timedelta(days=7)

    paid_today = isp.payments.filter(status="paid", paid_at__gte=day_ago)
    revenue_today = paid_today.aggregate(total=Sum("amount"))["total"] or Decimal("0")
    revenue_week = (
        isp.payments.filter(status="paid", paid_at__gte=week_ago).aggregate(total=Sum("amount"))["total"]
        or Decimal("0")
    )

    live = AccessGrant.objects.filter(isp=isp, state="active", expires_at__gt=now)
    expiring = live.filter(expires_at__lte=now + timedelta(minutes=30)).count()

    attempts = isp.payments.filter(created_at__gte=day_ago).count()
    settled = paid_today.count()
    # Compare like for like: only settled or failed attempts are a success rate.
    # A pending checkout is not a failure yet.
    decided = isp.payments.filter(created_at__gte=day_ago, status__in=["paid", "failed"]).count()
    success_rate = _pct(settled, decided)

    routers = list(isp.routers.all())
    chart = _daily_totals(isp, days=7, fmt="%a")

    by_package = (
        paid_today.values("package__name").annotate(count=Count("id"), total=Sum("amount")).order_by("-total")[:5]
    )

    context = {
        "active_nav": "overview",
        "isp": isp,
        "revenue_today": revenue_today,
        "revenue_week": revenue_week,
        "online_count": live.count(),
        "expiring_soon": expiring,
        "success_rate": success_rate,
        "attempts": attempts,
        "decided": decided,
        "settled": settled,
        "routers": routers,
        "chart": json.dumps(chart),
        "chart_max": max([row["total"] for row in chart], default=0) or 1,
        "by_package": by_package,
        "recent_payments": isp.payments.select_related("package").order_by("-created_at")[:6],
        "events": SessionEvent.objects.filter(isp=isp).select_related("grant")[:8],
    }
    return render(request, "admin/overview.html", context)


@panel_required
def live(request):
    isp = current_isp(request)
    now = timezone.now()
    grants = (
        AccessGrant.objects.filter(isp=isp, state="active", expires_at__gt=now)
        .select_related("customer", "package", "router")
        .order_by("expires_at")
    )
    devices = []
    router = isp.routers.filter(enabled=True).first()
    if router:
        try:
            result = mikrotik.online_users(router)
            devices = result.data or []
        except (mikrotik.RouterError, ValueError):
            devices = []
    return render(
        request,
        "admin/live.html",
        {"active_nav": "live", "isp": isp, "grants": grants, "devices": devices, "router": router},
    )


@panel_required
def network(request):
    isp = current_isp(request)
    routers = isp.routers.all()
    cards = []
    for router in routers:
        metrics = list(RouterMetric.objects.filter(router=router).order_by("-recorded_at")[:24])
        cards.append({"router": router, "latest": metrics[0] if metrics else None, "spark": list(reversed(metrics))})
    return render(request, "admin/network.html", {"active_nav": "network", "isp": isp, "cards": cards})


@panel_required
def customers(request):
    isp = current_isp(request)
    query = (request.GET.get("q") or "").strip()
    qs = Customer.objects.filter(isp=isp)
    if query:
        qs = qs.filter(mac_address__icontains=query) | qs.filter(phone__icontains=query)
    now = timezone.now()
    rows = []
    for customer in qs.order_by("-last_seen")[:100]:
        grant = (
            AccessGrant.objects.filter(customer=customer, state="active", expires_at__gt=now)
            .select_related("package")
            .first()
        )
        spent = (
            isp.payments.filter(customer=customer, status="paid").aggregate(total=Sum("amount"))["total"]
            or 0
        )
        rows.append({"customer": customer, "grant": grant, "spent": spent})
    return render(
        request,
        "admin/customers.html",
        {"active_nav": "customers", "isp": isp, "rows": rows, "q": query},
    )


@panel_required
def packages(request):
    isp = current_isp(request)
    items = isp.packages.all()
    return render(request, "admin/packages.html", {"active_nav": "packages", "isp": isp, "items": items})


@panel_required
def package_form(request, package_id=None):
    isp = current_isp(request)
    package = get_object_or_404(Package, pk=package_id, isp=isp) if package_id else None

    if request.method == "POST":
        data = request.POST
        instance = package or Package(isp=isp)
        instance.name = (data.get("name") or "").strip()[:80]
        instance.kind = data.get("kind", "hourly")
        instance.price = Decimal(data.get("price") or "0")
        units = data.get("duration_units", "hours")
        value = int(data.get("duration_value") or "0")
        instance.duration_minutes = value * {"minutes": 1, "hours": 60, "days": 1440}[units]
        instance.download_kbps = int(data.get("download_kbps") or 0)
        instance.upload_kbps = int(data.get("upload_kbps") or 0)
        instance.device_limit = int(data.get("device_limit") or 1)
        instance.is_active = data.get("is_active") == "on"
        instance.sort_order = int(data.get("sort_order") or 0)
        cap = (data.get("data_cap_mb") or "").strip()
        instance.data_cap_mb = int(cap) if cap and int(cap) > 0 else None
        instance.fup_action = data.get("fup_action") or "throttle"
        instance.fup_download_kbps = int(data.get("fup_download_kbps") or 256)
        instance.fup_upload_kbps = max(64, instance.fup_download_kbps // 2)
        instance.fup_profile = (data.get("fup_profile") or "").strip()[:60]
        if not instance.name or instance.duration_minutes <= 0:
            messages.error(request, "Give the package a name and a duration longer than zero.")
        else:
            instance.save()
            messages.success(request, "Saved" if package else "Package created")
            return redirect("panel:packages")

    # Split the stored minutes back into the form's number + unit pair.
    duration_value, duration_units = 1, "hours"
    if package:
        if package.duration_minutes % 1440 == 0:
            duration_value, duration_units = package.duration_minutes // 1440, "days"
        elif package.duration_minutes % 60 == 0:
            duration_value, duration_units = package.duration_minutes // 60, "hours"
        else:
            duration_value, duration_units = package.duration_minutes, "minutes"

    return render(
        request,
        "admin/package_form.html",
        {
            "active_nav": "packages",
            "isp": isp,
            "package": package,
            "duration_value": duration_value,
            "duration_units": duration_units,
        },
    )


@panel_required
def revenue(request):
    isp = current_isp(request)
    rows = _daily_totals(isp, days=30, fmt="%d %b")
    payments = isp.payments.select_related("package", "customer").order_by("-created_at")[:50]
    return render(
        request,
        "admin/revenue.html",
        {
            "active_nav": "revenue",
            "isp": isp,
            "paystack": getattr(isp, "paystack", None),
            "mpesa": getattr(isp, "mpesa", None),
            # Shortcode is encrypted at rest, so decode it for display only.
            "mpesa_till": (getattr(isp, "mpesa", None) and isp.mpesa.get_shortcode()) or "not set",
            "chart": json.dumps(rows),
            "chart_max": max([float(r["total"]) for r in rows], default=0) or 1,
            "total": sum(float(r["total"]) for r in rows),
            "payments": payments,
        },
    )


@panel_required
def settings_view(request):
    isp = current_isp(request)
    if request.method == "POST":
        section = request.POST.get("section")
        if section == "brand":
            isp.name = (request.POST.get("name") or isp.name)[:120]
            isp.support_phone = (request.POST.get("support_phone") or "")[:24]
            isp.currency = (request.POST.get("currency") or isp.currency)[:8]
            isp.save(update_fields=["name", "support_phone", "currency"])
            messages.success(request, "Saved")
        elif section == "paystack":
            from apps.tenants.models import PaystackConfig

            cfg, _ = PaystackConfig.objects.get_or_create(isp=isp)
            cfg.public_key = (request.POST.get("public_key") or "").strip()[:255]
            cfg.subaccount_code = (request.POST.get("subaccount_code") or "").strip()[:60]
            cfg.enabled = request.POST.get("enabled") == "on"
            if request.POST.get("secret_key"):
                cfg.set_secret(request.POST["secret_key"].strip())
            cfg.save()
            messages.success(request, "Paystack settings saved")
        elif section == "mpesa":
            from apps.tenants.models import MpesaConfig

            cfg, _ = MpesaConfig.objects.get_or_create(isp=isp)
            # Blank means "keep what is stored" - same rule as the secret fields,
            # so an operator can toggle M-Pesa without retyping the credentials.
            shortcode = (request.POST.get("shortcode") or "").strip()
            if shortcode:
                cfg.set_shortcode(shortcode[:12])
            consumer_key = (request.POST.get("consumer_key") or "").strip()
            if consumer_key:
                cfg.set_consumer_key(consumer_key[:255])
            cfg.enabled = request.POST.get("enabled") == "on"
            if request.POST.get("consumer_secret"):
                cfg.set_secret(request.POST["consumer_secret"].strip())
            if request.POST.get("passkey"):
                cfg.set_passkey(request.POST["passkey"].strip())
            cfg.save()
            messages.success(request, "M-Pesa settings saved")
        elif section == "router":
            from apps.tenants.models import Router

            router = get_object_or_404(Router, pk=request.POST.get("router_id"), isp=isp)
            router.name = (request.POST.get("router_name") or router.name)[:80]
            router.host = (request.POST.get("host") or router.host)[:120]
            router.port = int(request.POST.get("port") or router.port)
            router.api_user = (request.POST.get("api_user") or "")[:60]
            router.hotspot_profile = (request.POST.get("hotspot_profile") or router.hotspot_profile)[:60]
            if request.POST.get("api_password"):
                router.set_password(request.POST["api_password"])
            router.save()
            messages.success(request, "Router saved")
        return redirect("panel:settings")

    return render(
        request,
        "admin/settings.html",
        {
            "active_nav": "settings",
            "isp": isp,
            "routers": isp.routers.all(),
            "paystack": getattr(isp, "paystack", None),
            "mpesa": getattr(isp, "mpesa", None),
            "webhook_url": f"{settings.PUBLIC_BASE_URL}/payments/webhook/paystack/{isp.slug}/",
            "mpesa_callback_url": f"{settings.PUBLIC_BASE_URL}/payments/mpesa/callback/{isp.slug}/",
        },
    )


# --- actions ---------------------------------------------------------------
@panel_required
@require_POST
def extend_grant(request, grant_id):
    isp = current_isp(request)
    grant = get_object_or_404(AccessGrant, pk=grant_id, isp=isp)
    minutes = int(request.POST.get("minutes") or 0)
    if minutes <= 0:
        messages.error(request, "Enter how many minutes to add.")
        return redirect("panel:live")
    try:
        mikrotik.add_hotspot_user(
            grant.router,
            grant.customer.mac_address,
            profile=grant.router.hotspot_profile,
            minutes=grant.seconds_left // 60 + minutes,
            comment=f"pingota {grant.package.name}",
        )
        grant.expires_at = grant.expires_at + timedelta(minutes=minutes)
        grant.state = "active"
        grant.save(update_fields=["expires_at", "state"])
        SessionEvent.objects.create(
            isp=isp, grant=grant, event="extended", detail=f"+{minutes} minutes", router=grant.router
        )
        messages.success(request, f"Added {minutes} minutes")
    except (mikrotik.RouterError, ValueError) as exc:
        log.warning("extend grant failed for %s: %s", grant.id, exc)
        messages.error(
            request,
            "Could not add time. The router did not accept the change. Check the router connection and try again.",
        )
    return redirect("panel:live")


@panel_required
@require_POST
def revoke_grant_view(request, grant_id):
    isp = current_isp(request)
    grant = get_object_or_404(AccessGrant, pk=grant_id, isp=isp)
    revoke_grant(grant, "revoked by operator")
    messages.success(request, "Access revoked")
    return redirect("panel:live")


@panel_required
@require_POST
def test_router(request, router_id):
    from apps.tenants.models import Router

    isp = current_isp(request)
    router = get_object_or_404(Router, pk=router_id, isp=isp)
    try:
        result = mikrotik.health(router)
        router.last_status = "online"
        router.last_seen_at = timezone.now()
        router.save(update_fields=["last_status", "last_seen_at"])
        messages.success(request, f"Connected to {result.message}")
    except (mikrotik.RouterError, ValueError) as exc:
        log.warning("router test failed for %s: %s", router.id, exc)
        router.last_status = str(exc)[:120]
        router.save(update_fields=["last_status"])
        messages.error(
            request,
            "The router did not answer. Check that it is powered on and connected, then try again.",
        )
    return redirect("panel:settings")


@panel_required
@require_POST
def disconnect_device(request):
    isp = current_isp(request)
    mac = (request.POST.get("mac") or "").strip()
    router = isp.routers.filter(enabled=True).first()
    if not router:
        messages.error(request, "No router configured.")
        return redirect("panel:live")
    try:
        mikrotik.disconnect_only(router, mac)
        messages.success(request, f"Disconnected {mac}")
    except (mikrotik.RouterError, ValueError) as exc:
        log.warning("disconnect failed for %s: %s", mac, exc)
        messages.error(
            request,
            "The router did not accept the disconnect. Check the router connection and try again.",
        )
    return redirect("panel:live")


def csrf_failure(request, reason=""):
    return render(request, "portal/error.html", {"message": "Your session expired. Reload the page and try again."}, status=403)
