"""Request throttling and login brute-force protection.

The design follows Echowithin's security module: fixed-window counters scoped
to the client IP, plus a graduated account+IP lockout for sign-in. The counters
live in Django's cache so every worker sees the same budget; if the shared
cache is unreachable the in-process fallback keeps protection working instead
of failing open.

Nothing here trusts a forwarded header from an arbitrary peer. Proxy headers
are only read when the socket peer is a private/loopback address (the usual
Traefik/nginx container layout) or is listed in ``TRUSTED_PROXY_IPS``.
"""
from __future__ import annotations

import hashlib
import ipaddress
import logging
import re
import threading
import time
from functools import wraps

from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import render

log = logging.getLogger(__name__)

_MEMORY_LOCK = threading.Lock()
_MEMORY: dict[str, tuple[int, float]] = {}
_LAST_CACHE_WARNING = 0.0

# Sign-in rule: attempts 1-3 pass, 4-5 return 429 with a short pause, 6+ locks
# the account+IP pair for ten minutes. Success clears the pair.
_BF_CONFIG = {
    "login": {
        "window": 900,
        "lockout": 600,
        "friction_at": 4,
        "lockout_at": 6,
        "retry_4": 2,
        "retry_5": 4,
    },
}


def _limits_enabled() -> bool:
    return bool(getattr(settings, "SECURITY_LIMITS_ENABLED", True))


def _valid_ip(value: str) -> bool:
    try:
        ipaddress.ip_address((value or "").strip())
        return True
    except (ValueError, AttributeError):
        return False


def _public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address((value or "").strip())
    except (ValueError, AttributeError):
        return False
    return not (
        address.is_private
        or address.is_loopback
        or address.is_reserved
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
    )


def _trusted_peer(remote: str) -> bool:
    """Only honour forwarded IP headers from a proxy we can identify."""
    configured = getattr(settings, "TRUSTED_PROXY_IPS", []) or []
    if configured:
        return remote in configured
    try:
        address = ipaddress.ip_address(remote)
    except (ValueError, TypeError):
        return False
    return address.is_private or address.is_loopback or address.is_link_local


def get_client_ip(request) -> str:
    """Return the best client address available for this request.

    Cloudflare and Akamai headers are authoritative when present. Otherwise the
    leftmost public address in ``X-Forwarded-For`` is used, which is the value
    the edge proxy saw before any client-supplied hops.
    """
    if request is None:
        return "unknown"

    remote = (request.META.get("REMOTE_ADDR") or "").strip()
    if not getattr(settings, "TRUST_PROXY_HEADERS", True) or not _trusted_peer(remote):
        return remote if _valid_ip(remote) else "unknown"

    headers = request.headers
    for name in ("CF-Connecting-IP", "True-Client-IP"):
        candidate = (headers.get(name) or "").strip()
        if _valid_ip(candidate):
            return candidate

    x_real = (headers.get("X-Real-IP") or "").strip()
    if _public_ip(x_real):
        return x_real

    forwarded = headers.get("X-Forwarded-For") or ""
    candidates = [part.strip() for part in forwarded.split(",") if part.strip()]
    for candidate in candidates:
        if _public_ip(candidate):
            return candidate
    for candidate in candidates:
        if _valid_ip(candidate):
            return candidate

    if _valid_ip(x_real):
        return x_real
    return remote if _valid_ip(remote) else "unknown"


def _hash(value: str) -> str:
    return hashlib.sha256((value or "__unknown__").encode("utf-8")).hexdigest()[:16]


def _safe_ip(ip: str) -> str:
    return re.sub(r"[^0-9a-fA-F:.]", "_", ip or "unknown")[:64]


def _warn_cache_failure(exc: Exception) -> None:
    """Keep a dead cache visible without logging on every request."""
    global _LAST_CACHE_WARNING
    now = time.time()
    if now - _LAST_CACHE_WARNING < 60:
        return
    _LAST_CACHE_WARNING = now
    log.warning("security cache unavailable, using the in-process fallback: %s", exc)


def _memory_prune(now: float) -> None:
    if len(_MEMORY) < 5000:
        return
    for key, (_, expires_at) in list(_MEMORY.items()):
        if expires_at <= now:
            _MEMORY.pop(key, None)


def _memory_incr(key: str, expires_at: float) -> int:
    now = time.time()
    with _MEMORY_LOCK:
        _memory_prune(now)
        count, current_expiry = _MEMORY.get(key, (0, expires_at))
        if current_expiry <= now:
            count, current_expiry = 0, expires_at
        count += 1
        _MEMORY[key] = (count, current_expiry)
        return count


def _memory_get(key: str) -> tuple[int, float]:
    now = time.time()
    with _MEMORY_LOCK:
        entry = _MEMORY.get(key)
        if not entry:
            return 0, 0.0
        count, expires_at = entry
        if expires_at <= now:
            _MEMORY.pop(key, None)
            return 0, 0.0
        return count, expires_at


def check_rate_limit(
    request,
    scope: str,
    limit: int,
    window: int,
    *,
    identifier: str = "",
) -> tuple[bool, int]:
    """Count this request against ``scope`` for the client IP.

    Returns ``(allowed, retry_after_seconds)``. The counter is a fixed window
    keyed to the interval, so every worker increments the same bucket and the
    whole budget rolls over together.
    """
    if not _limits_enabled():
        return True, 0

    ip = _safe_ip(get_client_ip(request))
    now = time.time()
    bucket = int(now // window)
    suffix = f":{_hash(identifier)}" if identifier else ""
    key = f"rl:{scope}:{ip}{suffix}:{bucket}"
    retry_after = max(1, int(window - (now % window)))

    try:
        if cache.add(key, 1, window + 1):
            count = 1
        else:
            count = cache.incr(key)
    except Exception as exc:  # Redis down or cache backend misconfigured
        _warn_cache_failure(exc)
        count = _memory_incr(key, now + window + 1)

    if count > limit:
        log.warning(
            "rate_limit scope=%s ip=%s count=%s limit=%s window=%s path=%s",
            scope,
            ip,
            count,
            limit,
            window,
            getattr(request, "path", ""),
        )
        return False, retry_after
    return True, retry_after


def wait_label(seconds: int) -> str:
    seconds = max(1, int(seconds))
    if seconds < 60:
        return "1 second" if seconds == 1 else f"{seconds} seconds"
    minutes = (seconds + 59) // 60
    return "1 minute" if minutes == 1 else f"{minutes} minutes"


def rate_limited(
    request,
    retry_after: int,
    *,
    template: str = "429.html",
    context: dict | None = None,
    json: bool = False,
    retry_url: str = "",
    retry_label: str = "",
):
    wait = wait_label(retry_after)
    message = f"Too many tries. Wait {wait}, then try again."
    if json:
        response = JsonResponse(
            {"ok": False, "error": message, "retry_after": int(retry_after)},
            status=429,
        )
    else:
        payload = {
            "message": message,
            "retry_after": int(retry_after),
            "wait": wait,
            "retry_url": retry_url,
            "retry_label": retry_label,
        }
        if context:
            payload.update(context)
        response = render(request, template, payload, status=429)
    response["Retry-After"] = str(max(1, int(retry_after)))
    response["Cache-Control"] = "no-store"
    return response


def enforce_request_limit(
    request,
    scope: str,
    limit: int,
    window: int,
    *,
    identifier: str = "",
    template: str = "429.html",
    context: dict | None = None,
    json: bool = False,
    retry_url: str = "",
    retry_label: str = "",
):
    """Return a 429 response when the budget is spent, otherwise ``None``."""
    allowed, retry_after = check_rate_limit(
        request, scope, limit, window, identifier=identifier
    )
    if allowed:
        return None
    return rate_limited(
        request,
        retry_after,
        template=template,
        context=context,
        json=json,
        retry_url=retry_url,
        retry_label=retry_label,
    )


def rate_limit(
    scope: str,
    *,
    limit: int,
    window: int,
    methods: tuple[str, ...] | None = None,
    json: bool = False,
    template: str = "429.html",
    retry_url: str = "",
    retry_label: str = "",
):
    """Per-IP limit for a function view.

    ``methods`` counts only the listed verbs, so a page can render freely while
    its form posts are throttled.
    """

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if methods is not None and request.method not in methods:
                return view(request, *args, **kwargs)
            blocked = enforce_request_limit(
                request,
                scope,
                limit,
                window,
                json=json,
                template=template,
                retry_url=retry_url,
                retry_label=retry_label,
            )
            if blocked is not None:
                return blocked
            return view(request, *args, **kwargs)

        return wrapper

    return decorator


# --- Graduated sign-in protection -------------------------------------------

def _bf_key(kind: str, account: str, ip: str) -> str:
    return f"bf:{kind}:{_hash((account or '').strip().lower())}:{_safe_ip(ip)}"


def _bf_state(kind: str, account: str, ip: str) -> tuple[int, int]:
    cfg = _BF_CONFIG.get(kind, _BF_CONFIG["login"])
    key = _bf_key(kind, account, ip)
    now = time.time()
    try:
        count = int(cache.get(key) or 0)
        expires_at = cache.get(f"{key}:exp")
        if not count:
            return 0, 0
        ttl = int(float(expires_at) - now) if expires_at else cfg["window"]
        return count, max(0, ttl)
    except Exception as exc:
        _warn_cache_failure(exc)
        count, expires_at = _memory_get(key)
        return count, max(0, int(expires_at - now)) if count else 0


def _bf_record(kind: str, account: str, ip: str) -> tuple[int, int]:
    cfg = _BF_CONFIG.get(kind, _BF_CONFIG["login"])
    key = _bf_key(kind, account, ip)
    now = time.time()
    try:
        if cache.add(key, 1, cfg["window"]):
            count = 1
            expires_at = now + cfg["window"]
            cache.set(f"{key}:exp", expires_at, cfg["window"])
        else:
            count = int(cache.incr(key))
            expires_at = cache.get(f"{key}:exp")
            if not expires_at:
                expires_at = now + cfg["window"]
                cache.set(f"{key}:exp", expires_at, cfg["window"])
        if count >= cfg["lockout_at"]:
            expires_at = now + cfg["lockout"]
            cache.touch(key, cfg["lockout"])
            cache.set(f"{key}:exp", expires_at, cfg["lockout"])
        return count, max(1, int(float(expires_at) - now))
    except Exception as exc:
        _warn_cache_failure(exc)

    with _MEMORY_LOCK:
        count, expires_at = _MEMORY.get(key, (0, 0.0))
        if expires_at <= now:
            count, expires_at = 0, now + cfg["window"]
        count += 1
        if count >= cfg["lockout_at"]:
            expires_at = now + cfg["lockout"]
        _MEMORY[key] = (count, expires_at)
        return count, max(1, int(expires_at - now))


def _bf_tier(kind: str, count: int) -> str:
    cfg = _BF_CONFIG.get(kind, _BF_CONFIG["login"])
    if count >= cfg["lockout_at"]:
        return "lockout"
    if count >= cfg["friction_at"]:
        return "friction"
    return "ok"


def _bf_retry(kind: str, count: int, ttl: int) -> int:
    cfg = _BF_CONFIG.get(kind, _BF_CONFIG["login"])
    if count >= cfg["lockout_at"]:
        return max(1, ttl or cfg["lockout"])
    if count == 4:
        return cfg["retry_4"]
    if count == 5:
        return cfg["retry_5"]
    return 0


def brute_force_check(kind: str, account: str, ip: str) -> tuple[str, int, int, int]:
    """Return ``(tier, count, retry_after, ttl)`` without recording an attempt."""
    if not _limits_enabled():
        return "ok", 0, 0, 0
    count, ttl = _bf_state(kind, account, ip)
    tier = _bf_tier(kind, count)
    return tier, count, _bf_retry(kind, count, ttl), ttl


def brute_force_record_failure(kind: str, account: str, ip: str) -> tuple[str, int, int, int]:
    """Record a failed attempt and return the resulting tier state."""
    if not _limits_enabled():
        return "ok", 0, 0, 0
    count, ttl = _bf_record(kind, account, ip)
    tier = _bf_tier(kind, count)
    retry = _bf_retry(kind, count, ttl)
    account_hash = _hash((account or "").strip().lower())
    if tier == "friction":
        log.warning(
            "brute_force_friction kind=%s account_hash=%s ip=%s count=%s retry_after=%s",
            kind,
            account_hash,
            _safe_ip(ip),
            count,
            retry,
        )
    elif tier == "lockout":
        log.warning(
            "brute_force_lockout kind=%s account_hash=%s ip=%s count=%s ttl=%s",
            kind,
            account_hash,
            _safe_ip(ip),
            count,
            ttl,
        )
    return tier, count, retry, ttl


def brute_force_clear(kind: str, account: str, ip: str) -> None:
    """Clear the pair after a successful sign-in."""
    if not _limits_enabled():
        return
    key = _bf_key(kind, account, ip)
    try:
        cache.delete_many([key, f"{key}:exp"])
    except Exception as exc:
        _warn_cache_failure(exc)
    with _MEMORY_LOCK:
        _MEMORY.pop(key, None)


def begin_login(
    request,
    *,
    scope: str = "login",
    limit: int = 20,
    window: int = 60,
    kind: str = "login",
    template: str = "429.html",
    context: dict | None = None,
    retry_url: str = "",
    retry_label: str = "",
):
    """Rate-limit and check a sign-in POST before the password is verified.

    Returns ``(blocked_response, account, ip)``. ``blocked_response`` is None
    when the attempt may proceed.
    """
    blocked = enforce_request_limit(
        request,
        scope,
        limit,
        window,
        template=template,
        context=context,
        retry_url=retry_url,
        retry_label=retry_label,
    )
    account = (request.POST.get("username") or "").strip().lower()
    ip = get_client_ip(request)
    if blocked is not None:
        return blocked, account, ip

    tier, count, retry, ttl = brute_force_check(kind, account, ip)
    if tier == "lockout":
        log.warning(
            "brute_force_lockout kind=%s account_hash=%s ip=%s count=%s ttl=%s",
            kind,
            _hash(account),
            _safe_ip(ip),
            count,
            ttl,
        )
        return (
            rate_limited(
                request,
                retry,
                template=template,
                context=context,
                retry_url=retry_url,
                retry_label=retry_label,
            ),
            account,
            ip,
        )
    return None, account, ip


def finish_login(
    request,
    response,
    account: str,
    ip: str,
    *,
    kind: str = "login",
    template: str = "429.html",
    context: dict | None = None,
    retry_url: str = "",
    retry_label: str = "",
):
    """Clear the counter on success, or record the failure and maybe lock out."""
    if response.status_code in (301, 302):
        brute_force_clear(kind, account, ip)
        return response
    if response.status_code != 200:
        return response

    tier, count, retry, ttl = brute_force_record_failure(kind, account, ip)
    if tier in {"friction", "lockout"}:
        return rate_limited(
            request,
            retry,
            template=template,
            context=context,
            retry_url=retry_url,
            retry_label=retry_label,
        )
    return response
