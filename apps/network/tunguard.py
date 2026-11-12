"""Small client for the TunGuard peer-management API."""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

from django.conf import settings


class TunGuardError(RuntimeError):
    """The tunnel server is missing or rejected a peer operation."""


def _request(path: str, payload: dict | None = None) -> dict:
    base_url = settings.TUNGARD_API_URL.rstrip("/")
    api_key = settings.TUNGARD_API_KEY
    if not base_url or not api_key:
        raise TunGuardError("Router tunnel is not configured. Set TUNGARD_API_URL and TUNGARD_API_KEY.")

    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=data,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-API-Key": api_key,
        },
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            body = response.read(64 * 1024)
    except urllib.error.HTTPError as exc:
        detail = exc.read(2048).decode("utf-8", "replace").strip()
        raise TunGuardError(f"TunGuard returned HTTP {exc.code}: {detail or exc.reason}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise TunGuardError(f"Could not reach the router tunnel server: {exc}") from exc

    try:
        result = json.loads(body or b"{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TunGuardError("TunGuard returned an invalid response") from exc
    if not isinstance(result, dict) or result.get("success") is False:
        message = result.get("error", "peer operation failed") if isinstance(result, dict) else "invalid response"
        raise TunGuardError(f"TunGuard: {message}")
    return result


def server_public_key() -> str:
    """Return TunGuard's hex server key as RouterOS-required WireGuard base64."""
    value = _request("/api/server_key").get("public_key", "")
    try:
        raw = bytes.fromhex(value)
    except (TypeError, ValueError) as exc:
        raise TunGuardError("TunGuard returned an invalid server public key") from exc
    if len(raw) != 32:
        raise TunGuardError("TunGuard returned a server key that is not 32 bytes")
    return base64.b64encode(raw).decode("ascii")


def register_router(router) -> None:
    """Register a router peer using TunGuard's required hex key format."""
    try:
        public_key = base64.b64decode(router.tunnel_public_key, validate=True).hex()
    except (ValueError, TypeError) as exc:
        raise TunGuardError("Router public key is invalid") from exc
    _request(
        "/api/peer/add",
        {
            "public_key": public_key,
            "allowed_ip": f"{router.tunnel_address}/32",
            "device_name": f"Pingota: {router.isp.name} / {router.name}"[:100],
        },
    )


def remove_router(public_key: str) -> None:
    """Remove the router's peer from TunGuard."""
    if not public_key:
        return
    try:
        public_key_hex = base64.b64decode(public_key, validate=True).hex()
    except (ValueError, TypeError) as exc:
        raise TunGuardError("Router public key is invalid") from exc
    _request("/api/peer/remove", {"public_key": public_key_hex})
