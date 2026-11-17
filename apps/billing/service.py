"""Payment settlement -> access provisioning, in one place.

Every entry point (webhook, callback, demo checkout) funnels through
``settle_payment`` so the rules are identical and replayed callbacks are safe.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.billing.models import Payment, PlanPayment
from apps.network import mikrotik
from apps.network.models import SessionEvent
from apps.network.provisioning import ProvisioningDeferred, activate_grant, open_grant

log = logging.getLogger(__name__)


class SettlementError(RuntimeError):
    pass


def settle_payment(payment: Payment, *, provider_reference: str = "", raw: dict | None = None) -> Payment:
    """Idempotent: only the first call for a pending payment provisions access."""
    if payment.status == "paid":
        return payment

    # Re-read under a conditional update: two concurrent callbacks must not both
    # provision. The row count is the lock.
    updated = Payment.objects.filter(pk=payment.pk, status="pending").update(
        status="paid",
        paid_at=timezone.now(),
        provider_reference=provider_reference,
        raw_response=raw or {},
    )
    payment.refresh_from_db()
    if not updated:
        return payment  # someone else won the race and already handled it

    grant = open_grant(
        payment.isp,
        mac=payment.customer.mac_address if payment.customer else (raw or {}).get("mac", ""),
        package=payment.package,
        phone=payment.phone,
    )
    payment.grant = grant
    payment.save(update_fields=["grant"])

    try:
        activate_grant(grant, extend=payment.package.kind == "hourly")
    except ProvisioningDeferred as exc:
        # Paid but not yet online: the sweeper retries every tick.
        log.warning("provisioning deferred for grant %s: %s", grant.id, exc)
        paused = payment.isp.service_suspended
        SessionEvent.objects.create(
            isp=payment.isp,
            grant=grant,
            event="payment_error" if paused else "router_error",
            level="warn" if paused else "error",
            detail=(
                "Payment received while the network was paused. Access is held until the plan is restored."
                if paused
                else "Payment received. The router has not confirmed the pass yet, so the panel will retry."
            ),
        )
    return payment


def fail_payment(payment, reason: str) -> Payment:
    if payment.status == "pending":
        payment.status = "failed"
        payment.failure_reason = reason[:200]
        payment.save(update_fields=["status", "failure_reason"])
    return payment


def settle_plan_payment(
    payment: PlanPayment, *, provider_reference: str = "", raw: dict | None = None
) -> PlanPayment:
    """Idempotent hosting-plan activation.

    A renewal extends from the current end date when it is still in the future;
    an expired plan restarts from now. Manual suspension is a platform decision
    and is deliberately not lifted here.
    """
    if payment.status == "paid":
        return payment

    updated = PlanPayment.objects.filter(pk=payment.pk, status="pending").update(
        status="paid",
        paid_at=timezone.now(),
        provider_reference=provider_reference,
        raw_response=raw or {},
    )
    payment.refresh_from_db()
    if not updated:
        return payment

    now = timezone.now()
    isp = payment.isp
    start = max(now, isp.plan_ends_at) if isp.plan_ends_at else now
    end = start + timedelta(days=payment.plan.period_days)
    isp.plan = payment.plan.code
    isp.plan_ends_at = end
    isp.save(update_fields=["plan", "plan_ends_at"])

    payment.period_start = start
    payment.period_end = end
    payment.save(update_fields=["period_start", "period_end"])
    log.info(
        "plan %s activated for %s until %s (payment %s)",
        payment.plan.code,
        isp.slug,
        end.isoformat(),
        payment.reference,
    )
    return payment


def amount_matches(payment: Payment, amount) -> bool:
    """Compare whole shillings, the unit STK Push charges and Daraja echoes."""
    try:
        return int(Decimal(str(amount))) == int(Decimal(str(payment.amount)))
    except (ArithmeticError, ValueError, TypeError):
        return False


def mac_from_request(request) -> str:
    """Best-effort device identity.

    MikroTik's hotspot login page substitutes ``$(mac)`` server-side; the
    portal form posts that value. We also accept the header MikroTik may inject
    and never trust ARP on the server (a client can spoof X-Forwarded-For).
    """
    candidate = (
        request.POST.get("mac")
        or request.GET.get("mac")
        or request.session.get("portal_mac")
        or request.headers.get("X-Mikrotik-Mac", "")
    )
    try:
        return mikrotik.normalise_mac(candidate)
    except ValueError:
        return ""
