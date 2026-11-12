"""Turning a settled payment into working internet access.

Order of operations, and why:

1. Record the grant as ``active`` **only after** the router accepted it. If the
   router is down the payment stays paid and the grant stays pending, so the
   sweeper retries instead of the customer losing money silently.
2. Router calls happen outside the request that took the payment. A webhook
   returns in milliseconds; provisioning is idempotent because the hotspot
   username is derived from the MAC.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from apps.network import mikrotik
from apps.network.models import AccessGrant, Customer, SessionEvent

log = logging.getLogger(__name__)


class ProvisioningDeferred(RuntimeError):
    """Router did not accept the grant; leave it queued for the sweeper."""


def _log(isp, event, detail="", level="info", grant=None, router=None):
    SessionEvent.objects.create(isp=isp, grant=grant, router=router, event=event, level=level, detail=detail[:400])


def pick_router(isp):
    routers = [r for r in isp.routers.filter(enabled=True) if r.get_password()]
    return routers[0] if routers else None


def activate_grant(grant: AccessGrant, *, extend: bool = False) -> AccessGrant:
    """Enable the device on its router and persist the resulting state.

    ``extend`` appends the package duration to an existing live grant instead
    of restarting the clock — used when a customer buys a second pass.
    """
    router = grant.router or pick_router(grant.isp)
    if router is None:
        # Deliberately stays "active": the sweeper retries it. The customer has
        # paid, so the grant must not be closed because of our misconfiguration.
        grant.revoked_reason = "waiting for router"
        grant.save(update_fields=["revoked_reason"])
        _log(grant.isp, "router_error", "No usable router configured; access queued", "error", grant)
        raise ProvisioningDeferred("No router configured")

    grant.router = router
    # Router-side timer counts from now, so the DB clock must too.
    start = timezone.now()
    if extend and grant.state == "active" and grant.expires_at > start:
        start = grant.expires_at

    try:
        mikrotik.add_hotspot_user(
            router,
            grant.customer.mac_address,
            profile=router.hotspot_profile,
            minutes=grant.package.duration_minutes,
            comment=f"pingota {grant.package.name}",
        )
    except (mikrotik.RouterError, ValueError) as exc:
        # Stays ``active`` with the queued marker so the sweeper picks it up;
        # the customer has paid and must get access once the router returns.
        grant.revoked_reason = "waiting for router"
        grant.save(update_fields=["router", "revoked_reason"])
        _log(grant.isp, "router_error", f"{router.name}: {exc}", "error", grant, router)
        raise ProvisioningDeferred(str(exc)) from exc

    grant.state = "active"
    grant.started_at = start
    grant.expires_at = start + timedelta(minutes=grant.package.duration_minutes)
    grant.revoked_reason = ""
    grant.hotspot_username = mikrotik.hotspot_user_name(grant.customer.mac_address)
    grant.save(
        update_fields=["state", "started_at", "expires_at", "router", "hotspot_username", "revoked_reason"]
    )
    router.last_seen_at = timezone.now()
    router.last_status = "online"
    router.save(update_fields=["last_seen_at", "last_status"])
    _log(grant.isp, "connected", f"{grant.customer.mac_address} · {grant.package.name}", grant=grant, router=router)
    return grant


def open_grant(isp, mac: str, package, router=None, phone: str = "") -> AccessGrant:
    """Create the grant row for a paid package. Caller activates it."""
    mac = mikrotik.normalise_mac(mac)
    customer, _ = Customer.objects.get_or_create(isp=isp, mac_address=mac, defaults={"phone": phone})
    if phone and customer.phone != phone:
        customer.phone = phone
        customer.save(update_fields=["phone"])

    now = timezone.now()
    existing = (
        AccessGrant.objects.filter(isp=isp, customer=customer, state="active", expires_at__gt=now)
        .order_by("-expires_at")
        .first()
    )
    if existing and package.kind == "hourly":
        # Customer topped up while still online: extend the pass in place so the
        # router has exactly one user for the MAC and the sweeper one row.
        existing.package = package
        existing.expires_at = max(existing.expires_at, now) + timedelta(minutes=package.duration_minutes)
        existing.save(update_fields=["package", "expires_at"])
        return existing

    return AccessGrant.objects.create(
        isp=isp,
        customer=customer,
        package=package,
        router=router or pick_router(isp),
        state="active",
        started_at=now,
        expires_at=now,
    )


def revoke_grant(grant: AccessGrant, reason: str, *, keep_row: bool = True) -> None:
    """Remove access on the router, then mark the grant revoked/expired."""
    if grant.router:
        try:
            mikrotik.remove_access(grant.router, grant.customer.mac_address)
        except (mikrotik.RouterError, ValueError) as exc:
            _log(grant.isp, "router_error", f"{grant.router.name}: {exc}", "error", grant, grant.router)
            # The router-side timer still expires on its own, so do not block
            # the database transition on an unreachable router.
    event = "revoked" if reason == "revoked" else "expired"
    grant.revoked_reason = reason[:120]
    if keep_row:
        grant.state = "revoked" if reason == "revoked" else "expired"
        grant.save(update_fields=["state", "revoked_reason"])
    _log(grant.isp, event, reason, "info" if event == "expired" else "warn", grant, grant.router)


def expire_due_grants(now=None) -> int:
    """Sweeper body: close every grant whose clock ran out."""
    now = now or timezone.now()
    due = AccessGrant.objects.filter(state="active", expires_at__lte=now).select_related(
        "customer", "router", "isp"
    )[:200]
    count = 0
    for grant in due:
        revoke_grant(grant, "package ended")
        count += 1
    return count


def retry_pending_grants(limit: int = 20) -> int:
    """Grants paid for but never activated because the router was down."""
    queued = AccessGrant.objects.filter(
        state="active", revoked_reason="waiting for router", expires_at__gt=timezone.now()
    ).select_related("isp", "customer", "package", "router")[:limit]
    recovered = 0
    for grant in queued:
        try:
            activate_grant(grant)
            recovered += 1
        except ProvisioningDeferred:
            break  # still unreachable; try again next tick
    return recovered
