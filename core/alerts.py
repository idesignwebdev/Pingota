"""Push alerts via ntfy (https://ntfy.sh or self-hosted).

Sends background HTTP notifications for platform events:
- ISP signup requests
- Hosting plan purchases / renewals
- Router outages (transition from online to offline)
- Critical security lockouts (brute-force defense)

All calls are non-blocking and fail-safe: failures are logged and never
disrupt request processing or the scheduler loop.
"""
from __future__ import annotations

import logging
import threading

import httpx
from django.conf import settings

log = logging.getLogger(__name__)


def send_alert(
    title: str,
    message: str,
    *,
    priority: str = "default",
    tags: list[str] | None = None,
    click: str | None = None,
) -> None:
    """Send a notification to ntfy. Safe and non-blocking."""
    topic = getattr(settings, "NTFY_TOPIC", "")
    if not topic:
        return

    server = getattr(settings, "NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    token = getattr(settings, "NTFY_TOKEN", "")

    url = f"{server}/{topic}"
    headers = {
        "Title": title[:200],
        "Priority": priority,
    }
    if tags:
        headers["Tags"] = ",".join(tags)
    if click:
        headers["Click"] = click
    if token:
        headers["Authorization"] = f"Bearer {token}"

    def _deliver():
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.post(url, content=message.encode("utf-8"), headers=headers)
                if resp.status_code >= 400:
                    log.warning("ntfy alert failed: HTTP %s (%s)", resp.status_code, resp.text)
        except Exception as exc:
            log.warning("ntfy delivery failed: %s", exc)

    # Deliver in a daemon thread so the caller is never slowed down
    threading.Thread(target=_deliver, daemon=True).start()


def notify_signup(signup) -> None:
    """Alert platform owner when an ISP requests an account."""
    base_url = getattr(settings, "PUBLIC_BASE_URL", "").rstrip("/")
    admin_url = (
        f"{base_url}/django-admin/accounts/signuprequest/{signup.pk}/change/"
        if base_url
        else ""
    )
    send_alert(
        title="New ISP Signup Request",
        message=(
            f"Business: {signup.business_name}\n"
            f"Contact: {signup.contact_name or 'N/A'}\n"
            f"Phone: {signup.phone} | Email: {signup.email}\n"
            f"County: {signup.county or 'N/A'} | Routers: {signup.routers}"
        ),
        priority="high",
        tags=["memo", "inbox_tray"],
        click=admin_url or None,
    )


def notify_plan_payment(payment) -> None:
    """Alert when an ISP pays for a hosting plan."""
    send_alert(
        title="Hosting Plan Payment Received",
        message=(
            f"{payment.isp.name} paid {payment.currency} {payment.amount:,.0f} for "
            f"Plan '{payment.plan.name}' via {payment.provider.upper()}."
        ),
        priority="high",
        tags=["moneybag", "white_check_mark"],
    )


def notify_router_down(router, reason: str = "") -> None:
    """Alert when a previously online router goes offline."""
    send_alert(
        title="Router Offline Alert",
        message=f"Router '{router.name}' ({router.isp.name}) is unreachable: {reason}",
        priority="urgent",
        tags=["warning", "satellite"],
    )


def notify_security_lockout(kind: str, ip: str, count: int) -> None:
    """Alert on potential brute force attack lockout."""
    send_alert(
        title="Security Lockout Alert",
        message=f"IP {ip} locked out after {count} failed attempts on '{kind}'.",
        priority="high",
        tags=["shield", "rotating_light"],
    )
