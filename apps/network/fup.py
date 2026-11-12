"""Fair use policy enforcement.

Runs on the same short loop as expiry. For every live grant with a data cap it
reads the device's byte counters off the router, adds them to the grant's stored
total, and applies the package's FUP action the first time the cap is crossed:

* ``throttle`` — move the hotspot user to a slow profile; the customer stays
  online, just slower. This is what most Kenyan ISPs advertise as FUP.
* ``block`` — remove the user and close the grant; the guest must buy again.

Bytes are converted to MB with 1024-based units, matching what a customer sees
on their phone.
"""
from __future__ import annotations

import logging

from django.utils import timezone

from apps.network import mikrotik
from apps.network.models import AccessGrant, SessionEvent

log = logging.getLogger(__name__)

MB = 1024 * 1024


def sync_usage(router, grants: list[AccessGrant]) -> int:
    """Pull counters for ``grants`` and store the deltas.

    Returns the number of grants updated, or ``None`` if the router could not be
    reached — the caller uses that to back off instead of re-dialling every
    tick.
    """
    if not grants:
        return 0
    try:
        usage = mikrotik.usage_by_user(router)
    except (mikrotik.RouterError, ValueError) as exc:
        log.warning("usage sync failed for %s: %s", router, exc)
        return None

    # The grant stores what we have already counted (`bytes_synced`), so each
    # tick adds only the difference. A router reboot resets its counters; a
    # reading smaller than last time is treated as a fresh counter.
    updated = 0
    for grant in grants:
        username = grant.hotspot_username or mikrotik.hotspot_user_name(grant.customer.mac_address)
        seen = usage.get(username)
        if seen is None:
            continue
        delta = seen - grant.bytes_synced if seen >= grant.bytes_synced else seen
        if delta <= 0:
            continue
        grant.bytes_synced = seen
        grant.data_used_mb += delta // MB
        grant.save(update_fields=["bytes_synced", "data_used_mb"])
        updated += 1
    return updated


def enforce(grants_router_pairs) -> tuple[int, set]:
    """Apply FUP for capped grants.

    Returns ``(acted, unreachable_router_ids)`` so the scheduler can stop
    dialling a router that just timed out.
    """
    acted = 0
    unreachable: set = set()
    for router, grants in grants_router_pairs:
        if not grants:
            continue
        if sync_usage(router, grants) is None:
            unreachable.add(str(router.pk))
            continue  # no counters, so no policy decision this tick
        for grant in grants:
            package = grant.package
            if not package.data_cap_mb or grant.fup_state != "ok":
                continue
            if grant.data_used_mb < package.data_cap_mb:
                continue
            try:
                acted += _apply(router, grant)
            except (mikrotik.RouterError, ValueError) as exc:
                log.warning("FUP action failed for %s: %s", grant.customer.mac_address, exc)
                unreachable.add(str(router.pk))
    return acted, unreachable


def _apply(router, grant: AccessGrant) -> int:
    package = grant.package
    if package.fup_action == "block":
        from apps.network.provisioning import revoke_grant

        revoke_grant(grant, "data allowance used")
        grant.fup_state = "blocked"
        grant.save(update_fields=["fup_state"])
        _event(grant, "Allowance of {:,} MB used · pass closed".format(package.data_cap_mb), "fup_blocked")
        return 1

    profile = package.fup_profile or router.hotspot_profile
    mikrotik.apply_profile(router, grant.customer.mac_address, profile)
    grant.fup_state = "throttled"
    grant.save(update_fields=["fup_state"])
    _event(
        grant,
        "Allowance of {:,} MB used · moved to {} kbps".format(
            package.data_cap_mb, package.fup_download_kbps or "slow"
        ),
        "fup_throttled",
    )
    return 1


def restore(grant: AccessGrant) -> None:
    """Operator reset the allowance, e.g. after a false throttle."""
    grant.fup_state = "ok"
    grant.data_used_mb = 0
    grant.bytes_synced = 0
    grant.save(update_fields=["fup_state", "data_used_mb", "bytes_synced"])


def _event(grant, detail: str, event_kind: str) -> None:
    SessionEvent.objects.create(
        isp=grant.isp,
        grant=grant,
        router=grant.router,
        event=event_kind,
        level="warn",
        detail=detail[:400],
        created_at=timezone.now(),
    )
