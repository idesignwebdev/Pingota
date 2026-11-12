"""In-process sweeper.

One background thread per web container, started once from AppConfig.ready().
It does three things on a short loop:

* close grants whose time ran out (and remove them from the router)
* retry provisioning for paid grants whose router was unreachable
* sample router health once a minute

It is deliberately not Celery: a second broker, worker container and restart
policy is a lot of moving parts for three small jobs. If the loop ever stops
being enough, set SCHEDULER_ENABLED=false and run ``python manage.py tick`` from
cron — the same code path.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time

from django.conf import settings

log = logging.getLogger(__name__)

_started = False
_lock = threading.Lock()

#: router id -> monotonic time before which it is not worth dialling again.
#: A router that just timed out should not be retried every tick: each attempt
#: costs a full connect timeout and starves the other routers of time.
_backoff: dict[str, float] = {}
BACKOFF_BASE = 30.0
BACKOFF_MAX = 900.0
_failures: dict[str, int] = {}


def _reachable(router, now: float) -> bool:
    return _backoff.get(str(router.pk), 0.0) <= now


def _note_result(router, ok: bool, now: float) -> None:
    key = str(router.pk)
    if ok:
        _failures.pop(key, None)
        _backoff.pop(key, None)
        return
    count = _failures.get(key, 0) + 1
    _failures[key] = count
    # 30s, 60s, 120s ... capped at 15 minutes.
    delay = min(BACKOFF_BASE * (2 ** (count - 1)), BACKOFF_MAX)
    _backoff[key] = now + delay

# Management commands that must never spin up the sweeper.
_NO_SCHEDULER_COMMANDS = {
    "migrate",
    "makemigrations",
    "collectstatic",
    "shell",
    "test",
    "seed_demo",
    "bootstrap",
    "tick",
    "createsuperuser",
    "check",
}


def tick() -> dict:
    """Run one pass of all jobs. Also used by the ``tick`` management command."""
    from apps.network import mikrotik
    from apps.network.provisioning import expire_due_grants, retry_pending_grants
    from apps.tenants.models import Router

    result = {"expired": 0, "recovered": 0, "routers": 0}
    try:
        result["expired"] = expire_due_grants()
    except Exception:  # noqa: BLE001 - one bad row must not kill the loop
        log.exception("expiry sweep failed")
    try:
        result["recovered"] = retry_pending_grants()
    except Exception:  # noqa: BLE001
        log.exception("provisioning retry failed")
    try:
        for router in Router.objects.filter(enabled=True):
            mikrotik.record_health(router)
            result["routers"] += 1
    except Exception:  # noqa: BLE001
        log.exception("router health sampling failed")
    return result


def start() -> None:
    """Start the loop if this process should run it (not during migrations)."""
    global _started
    if not settings.SCHEDULER_ENABLED:
        return
    # Gunicorn imports the app in workers plus the master; RUN_SCHEDULER lets
    # you pin it to one container when scaling horizontally.
    if os.environ.get("RUN_SCHEDULER", "true").lower() not in {"1", "true", "yes"}:
        return
    command = (sys.argv[1] if len(sys.argv) > 1 else "").lstrip("-")
    if command in _NO_SCHEDULER_COMMANDS:
        return
    # Under the test runner the sweeper would fire against the test database
    # and keep the process alive after the last test.
    if "test" in sys.argv or os.environ.get("PYTEST_CURRENT_TEST"):
        return

    with _lock:
        if _started:
            return
        _started = True

    def loop():
        last_health = 0.0
        log.info("scheduler started (expiry %ss, health %ss)", settings.EXPIRY_TICK_SECONDS, settings.SYNC_TICK_SECONDS)
        while True:
            now = time.monotonic()
            try:
                from apps.network.provisioning import expire_due_grants, retry_pending_grants

                expire_due_grants()
                retry_pending_grants(limit=5)
                _enforce_fup()
                if now - last_health >= settings.SYNC_TICK_SECONDS:
                    last_health = now
                    _sample_routers()
            except Exception:  # noqa: BLE001
                log.exception("scheduler tick failed")
            time.sleep(settings.EXPIRY_TICK_SECONDS)

    threading.Thread(target=loop, name="pingota-sweeper", daemon=True).start()


def _enforce_fup() -> None:
    """Sample usage for capped, live grants and apply the package policy."""
    from django.utils import timezone

    from apps.network.fup import enforce
    from apps.network.models import AccessGrant
    from apps.tenants.models import Router

    now = time.monotonic()
    pairs = []
    for router in Router.objects.filter(enabled=True, status="connected")[:50]:
        if not _reachable(router, now):
            continue  # still backing off after a failed dial
        grants = list(
            AccessGrant.objects.filter(
                isp=router.isp,
                router=router,
                state="active",
                expires_at__gt=timezone.now(),
                package__data_cap_mb__isnull=False,
            ).select_related("package", "customer")[:200]
        )
        if grants:
            pairs.append((router, grants))
    try:
        acted, unreachable = enforce(pairs)
        for router, _grants in pairs:
            _note_result(router, str(router.pk) not in unreachable, now)
        return acted
    except Exception:  # noqa: BLE001 - FUP must never stop expiry
        log.exception("FUP enforcement failed")


def _sample_routers() -> None:
    from apps.network import mikrotik
    from apps.tenants.models import Router

    now = time.monotonic()
    for router in Router.objects.filter(enabled=True)[:50]:
        if not _reachable(router, now):
            continue
        mikrotik.record_health(router)
        router.refresh_from_db(fields=["last_status"])
        _note_result(router, router.last_status == "online", now)
