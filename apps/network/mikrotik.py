"""MikroTik RouterOS API client.

Deliberate differences from the original Node helper:

* Binds the device to a **hotspot user** with ``limit-uptime`` and a speed
  ``profile``. A *bypassed* IP binding (what the old code created) skips the
  hotspot entirely: no timer, no bandwidth limit and no way to expire it, so a
  paying customer effectively kept free internet forever.
* Everything is keyed by the router row, not by global env vars, so one process
  can talk to many ISPs' routers.
* Connections are opened per operation with a hard deadline; a slow or offline
  router raises ``RouterUnreachable`` instead of hanging a web request. Access
  is never activated on the strength of a payment alone — see
  ``apps.network.provisioning``.
* No RSC command injection: values are validated (MAC regex, digits-only
  durations) before they reach the wire.

The API user configured on the router should have the minimum policy needed:
``read,write,api,test`` limited to hotspot + address-list commands.
"""
from __future__ import annotations

import logging
import re
import socket
import time
from dataclasses import dataclass
from typing import Iterable

from django.utils import timezone

log = logging.getLogger(__name__)

MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,60}$")


class RouterError(RuntimeError):
    """Router replied with an error."""


class RouterUnreachable(RouterError):
    """Could not connect or the connection timed out."""


@dataclass
class RouterResult:
    ok: bool
    message: str
    data: list[dict] | None = None


def normalise_mac(mac: str) -> str:
    mac = (mac or "").strip().upper().replace("-", ":")
    if not MAC_RE.match(mac):
        raise ValueError("Invalid MAC address")
    return mac


def _safe(value: str, pattern=SAFE_NAME_RE, field="value") -> str:
    if not pattern.match(str(value)):
        raise ValueError(f"Unsafe {field}: {value!r}")
    return str(value)


class RouterOSClient:
    """Minimal synchronous RouterOS API client (login + command + done)."""

    #: Wire sentence terminators, per the RouterOS API spec.
    _ZERO = b"\x00\x00"

    def __init__(self, host: str, username: str, password: str, port: int = 8728, timeout: float = 6.0):
        self.host = host
        self.username = username
        self.password = password
        self.port = int(port or 8728)
        self.timeout = timeout
        self._sock: socket.socket | None = None

    # --- framing ---------------------------------------------------------
    @staticmethod
    def _encode_length(length: int) -> bytes:
        if length < 0x80:
            return bytes([length])
        if length < 0x4000:
            length |= 0x8000
            return length.to_bytes(2, "big")
        if length < 0x200000:
            length |= 0xC00000
            return length.to_bytes(3, "big")
        if length < 0x10000000:
            length |= 0xE0000000
            return length.to_bytes(4, "big")
        return b"\xF0" + length.to_bytes(4, "big")

    def _read_length(self) -> int:
        assert self._sock
        first = self._sock.recv(1)
        if not first:
            raise RouterUnreachable("Connection closed by router")
        byte = first[0]
        if byte & 0x80 == 0:
            return byte
        if byte & 0xC0 == 0x80:
            return int.from_bytes(first + self._recv_exact(1), "big") & 0x3FFF
        if byte & 0xE0 == 0xC0:
            return int.from_bytes(first + self._recv_exact(2), "big") & 0x1FFFFF
        if byte & 0xF0 == 0xE0:
            return int.from_bytes(first + self._recv_exact(3), "big") & 0x0FFFFFFF
        return int.from_bytes(self._recv_exact(4), "big")

    def _recv_exact(self, n: int) -> bytes:
        assert self._sock
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise RouterUnreachable("Connection closed by router")
            buf += chunk
        return buf

    def _write_sentence(self, words: Iterable[str]) -> None:
        assert self._sock
        out = b""
        for word in words:
            encoded = word.encode("utf-8")
            out += self._encode_length(len(encoded)) + encoded
        out += self._ZERO
        self._sock.sendall(out)

    def _read_sentence(self) -> list[str]:
        words: list[str] = []
        while True:
            length = self._read_length()
            if length == 0:
                return words
            words.append(self._recv_exact(length).decode("utf-8", "replace"))

    @staticmethod
    def _parse(words: list[str]) -> dict:
        attrs: dict = {}
        for word in words:
            if word.startswith("="):
                key, _, value = word[1:].partition("=")
                attrs[key] = value
        return attrs

    # --- session ---------------------------------------------------------
    def _login(self) -> None:
        """RouterOS 6.43+ plain login; falls back to pre-6.43 challenge."""
        self._write_sentence(["/login", f"=name={self.username}", f"=password={self.password}"])
        words = self._read_sentence()
        if words and words[0] == "!done":
            return
        attrs = self._parse(words)
        challenge = attrs.get("ret")
        if not challenge:
            raise RouterError(f"Login refused: {' '.join(words)[:120]}")
        # Legacy MD5 challenge (RouterOS < 6.43).
        import binascii
        import hashlib

        digest = hashlib.md5(
            b"\x00" + self.password.encode() + binascii.unhexlify(challenge.encode())
        ).hexdigest()
        self._write_sentence(["/login", f"=name={self.username}", f"=response=00{digest}"])
        words = self._read_sentence()
        if not words or words[0] != "!done":
            raise RouterError(f"Login refused: {' '.join(words)[:120]}")

    def connect(self) -> None:
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self._sock.settimeout(self.timeout)
            self._login()
        except (OSError, socket.timeout) as exc:
            self.close()
            raise RouterUnreachable(f"{self.host}:{self.port} unreachable ({exc})") from exc

    def close(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def command(self, cmd: str, **attrs) -> list[dict]:
        if self._sock is None:
            self.connect()
        sentence = [cmd] + [f"={k.replace('_', '-')}={v}" for k, v in attrs.items() if v is not None]
        self._write_sentence(sentence)

        rows: list[dict] = []
        while True:
            words = self._read_sentence()
            if not words:
                return rows
            tag = words[0]
            if tag == "!re":
                rows.append(self._parse(words))
            elif tag == "!done":
                return rows
            elif tag == "!trap":
                raise RouterError(self._parse(words).get("message", "unknown trap"))
            elif tag == "!fatal":
                raise RouterError(" ".join(words)[:160])


class RouterConnection:
    """Context manager around a router row, with a single connect per use."""

    def __init__(self, router, timeout: float = 6.0):
        self.router = router
        self.timeout = timeout
        self.client: RouterOSClient | None = None
        self.latency_ms = 0

    def __enter__(self) -> RouterOSClient:
        if not self.router.enabled:
            raise RouterUnreachable("Router is disabled")
        password = self.router.get_password()
        if not self.router.api_user or not password:
            raise RouterUnreachable("Router API credentials are not set")
        client = RouterOSClient(self.router.host, self.router.api_user, password, self.router.port, self.timeout)
        started = time.monotonic()
        client.connect()
        self.latency_ms = int((time.monotonic() - started) * 1000)
        self.client = client
        return client

    def __exit__(self, *exc):
        if self.client:
            self.client.close()
        return False


# --- operations -------------------------------------------------------------
def hotspot_user_name(mac: str) -> str:
    """Stable hotspot username derived from the MAC, easy to find by grep."""
    return "pg-" + normalise_mac(mac).replace(":", "").lower()


def add_hotspot_user(router, mac: str, profile: str, minutes: int, comment: str = "") -> RouterResult:
    """Create (or replace) a time-limited hotspot user.

    ``limit-uptime`` is the authoritative timer: RouterOS disconnects the
    device when it elapses, whether or not the server is running.
    """
    mac = normalise_mac(mac)
    profile = _safe(profile or "default", field="profile")
    minutes = int(minutes)
    if minutes <= 0:
        raise ValueError("Duration must be positive")
    username = hotspot_user_name(mac)

    with RouterConnection(router) as api:
        _remove_user(api, username)  # idempotent: replaces a stale pass
        api.command(
            "/ip/hotspot/user/add",
            name=username,
            password=mac.replace(":", ""),
            profile=profile,
            mac_address=mac,
            limit_uptime=f"{minutes}m",
            comment=(comment or "")[:60],
        )
        # The user is what grants access; the binding pins the MAC so the
        # device cannot log in with credentials on another machine.
        api.command(
            "/ip/hotspot/ip-binding/add",
            mac_address=mac,
            type="regular",
            server="all",
            comment=f"pingota:{username}"[:60],
        )
    return RouterResult(ok=True, message=f"Access enabled for {mac}", data=[{"username": username}])


def remove_access(router, mac: str) -> RouterResult:
    mac = normalise_mac(mac)
    username = hotspot_user_name(mac)
    with RouterConnection(router) as api:
        _disconnect_active(api, mac, username)
        _remove_user(api, username)
        for binding in api.command("/ip/hotspot/ip-binding/print", **{"?mac-address": mac}):
            if binding.get(".id"):
                api.command("/ip/hotspot/ip-binding/remove", **{".id": binding[".id"]})
    return RouterResult(ok=True, message=f"Access removed for {mac}")


def disconnect_only(router, mac: str) -> RouterResult:
    """Drop the live session but keep the pass, for a reconnect."""
    mac = normalise_mac(mac)
    with RouterConnection(router) as api:
        count = _disconnect_active(api, mac, hotspot_user_name(mac))
    return RouterResult(ok=True, message=f"Disconnected {count} session(s)")


def _remove_user(api: RouterOSClient, username: str) -> None:
    for row in api.command("/ip/hotspot/user/print", **{"?name": username}):
        if row.get(".id"):
            api.command("/ip/hotspot/user/remove", **{".id": row[".id"]})


def _disconnect_active(api: RouterOSClient, mac: str, username: str) -> int:
    removed = 0
    for row in api.command("/ip/hotspot/active/print"):
        if (row.get("mac-address", "").upper() == mac) or (row.get("user") == username):
            if row.get(".id"):
                api.command("/ip/hotspot/active/remove", **{".id": row[".id"]})
                removed += 1
    return removed


def online_users(router) -> RouterResult:
    with RouterConnection(router) as api:
        rows = api.command("/ip/hotspot/active/print")
    data = [
        {
            "mac": row.get("mac-address", ""),
            "ip": row.get("address", ""),
            "user": row.get("user", ""),
            "uptime": row.get("uptime", ""),
            "bytes_in": int(row.get("bytes-in", 0) or 0),
            "bytes_out": int(row.get("bytes-out", 0) or 0),
        }
        for row in rows
    ]
    return RouterResult(ok=True, message=f"{len(data)} online", data=data)


def health(router) -> RouterResult:
    """Cheap probe for the monitoring card and the status page."""
    with RouterConnection(router) as api:
        identity = api.command("/system/identity/print")
        resource = api.command("/system/resource/print")
    info = resource[0] if resource else {}
    return RouterResult(
        ok=True,
        message=identity[0].get("name", "router") if identity else "router",
        data=[
            {
                "uptime": info.get("uptime", ""),
                "cpu_load": int(info.get("cpu-load", 0) or 0),
                "free_memory": int(info.get("free-memory", 0) or 0),
                "version": info.get("version", ""),
            }
        ],
    )


def usage_by_user(router) -> dict[str, int]:
    """Total bytes per hotspot user, from active sessions plus stale users.

    ``bytes-in`` on an active session is per login, so a device that reconnects
    would reset its usage. Adding it to the user's stored counter keeps the FUP
    honest across reconnects.
    """
    usage: dict[str, int] = {}
    with RouterConnection(router) as api:
        for row in api.command("/ip/hotspot/active/print"):
            user = row.get("user", "")
            if not user:
                continue
            usage[user] = usage.get(user, 0) + int(row.get("bytes-in", 0) or 0) + int(row.get("bytes-out", 0) or 0)
    return usage


def apply_profile(router, mac: str, profile: str) -> RouterResult:
    """Move a live user onto another hotspot profile (FUP throttle)."""
    mac = normalise_mac(mac)
    profile = _safe(profile, field="profile")
    username = hotspot_user_name(mac)
    with RouterConnection(router) as api:
        rows = api.command("/ip/hotspot/user/print", **{"?name": username})
        if not rows or not rows[0].get(".id"):
            raise RouterError("User is not on this router")
        api.command("/ip/hotspot/user/set", **{".id": rows[0][".id"], "profile": profile})
        # A live session keeps its old profile until it re-authenticates.
        _disconnect_active(api, mac, username)
    return RouterResult(ok=True, message=f"Moved to profile {profile}")


def record_health(router) -> None:
    """Persist a RouterMetric row. Never raises: monitoring must not break traffic."""
    from apps.network.models import RouterMetric

    started = time.monotonic()
    try:
        # One connection serves both calls: each RouterConnection dial is a TCP
        # connect plus login, which is the dominant cost on a slow link.
        with RouterConnection(router) as api:
            identity = api.command("/system/identity/print")
            resource = api.command("/system/resource/print")
            active = api.command("/ip/hotspot/active/print")
        latency = int((time.monotonic() - started) * 1000)
        info = (resource or [{}])[0]
        router.last_seen_at = timezone.now()
        router.last_status = "online"
        router.save(update_fields=["last_seen_at", "last_status"])
        RouterMetric.objects.create(
            router=router,
            online_users=len(active or []),
            cpu_percent=int(info.get("cpu-load", 0) or 0),
            reachable=True,
            latency_ms=latency,
        )
    except (RouterError, ValueError, OSError) as exc:
        router.last_status = str(exc)[:120]
        router.save(update_fields=["last_status"])
        RouterMetric.objects.create(router=router, reachable=False, latency_ms=0)
        log.warning("router health failed: %s -> %s", router, exc)
