"""Self-service router onboarding.

An ISP adds a router in the panel. Pingota generates everything the router needs
and hands back one command block to paste into the RouterOS terminal:

* a WireGuard key pair — the private key stays encrypted on the server, the
  public key is registered with TunGuard, and the private key is included only
  in the one-time install script;
* a reserved address in the tunnel subnet, so a router behind CGNAT needs no
  port forward and no public IP;
* a restricted API user with a random password;
* an install token that lets the router report which version it runs and that
  it really applied the config.

Nothing is trusted until that report arrives: the router shows "waiting for
install" until then.
"""
from __future__ import annotations

import base64
import ipaddress
import secrets

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from django.conf import settings
from django.utils import timezone

from apps.network.models import TUNNEL_SUBNET

#: RouterOS policy list for the API user: enough to manage hotspot users and
#: address lists, nothing that lets it rebuild or reboot the router.
API_POLICY = "read,write,api,test,!ftp,!reboot,!password,!policy,!sensitive,!romon"


def new_x25519() -> tuple[str, str]:
    """Return (private_b64, public_b64) in WireGuard's 32-byte base64 form."""
    private = X25519PrivateKey.generate()
    priv_raw = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(priv_raw).decode(), base64.b64encode(pub_raw).decode()


def allocate_tunnel_address() -> str:
    """Pick the next unused router IP from the shared tunnel subnet."""
    from apps.tenants.models import Router

    base = ipaddress.ip_network(TUNNEL_SUBNET)
    used = {
        ipaddress.ip_address(addr)
        for addr in Router.objects.exclude(tunnel_address=None).values_list("tunnel_address", flat=True)
        if addr
    }
    server_ip = base.network_address + 1
    for address in base.hosts():
        if address != server_ip and address not in used:
            return str(address)
    raise RuntimeError("Router tunnel address pool is full; increase ROUTER_TUNNEL_SUBNET")


def provision_identity(router) -> None:
    """Fill in tunnel keys, API password, token and address for a new router."""
    from core.crypto import encrypt

    private_key, public_key = new_x25519()
    router.tunnel_public_key = public_key
    router.set_tunnel_private_key(private_key)
    router.api_user = f"pingota-{secrets.token_hex(2)}"
    router.api_password = encrypt(secrets.token_urlsafe(24))
    router.install_token = secrets.token_urlsafe(32)
    router.tunnel_address = allocate_tunnel_address()
    router.host = str(router.tunnel_address)
    router.status = "pending"


def server_public_key() -> str:
    """Public half of TunGuard's server key, formatted for RouterOS."""
    from apps.network.tunguard import server_public_key as get_tunguard_public_key

    return get_tunguard_public_key()


def register_tunnel_peer(router) -> None:
    from apps.network.tunguard import register_router

    register_router(router)


def remove_tunnel_peer(public_key: str) -> None:
    from apps.network.tunguard import remove_router

    remove_router(public_key)


def install_script(router) -> str:
    """RouterOS commands the ISP pastes in one go.

    TunGuard registers each peer before this script is shown. The block is
    idempotent: it replaces the tunnel, API user and firewall rule.
    """
    from core.crypto import decrypt

    if not router.tunnel_public_key or not router.install_token:
        raise ValueError("Router identity has not been generated yet")
    api_password = decrypt(router.api_password)
    if not api_password:
        raise ValueError("Router API password is missing; recreate the router to regenerate it")
    private_key = decrypt(router.tunnel_private_key)
    if not private_key:
        raise ValueError("Router tunnel private key is missing; regenerate credentials")

    tunnel = ipaddress.ip_network(TUNNEL_SUBNET)
    server_ip = str(tunnel.network_address + 1)
    report = f"{settings.PUBLIC_BASE_URL}/routers/{router.install_token}/report/"
    # RouterOS evaluates $[] expressions inside tool/fetch URLs before sending.
    version = "$[/system/resource/get value-name=version]"
    board = "$[/system/identity/get value-name=name]"

    return f"""# Pingota install — {router.isp.name} / {router.name}
# RouterOS 7. Paste this whole block, then wait about ten seconds.
# Safe to run twice: the tunnel, API user and firewall rule are replaced.

/interface/wireguard/remove [find name="pingota-wg"]
/user/remove [find name="{router.api_user}"]
/user/group/remove [find name="pingota"]
/ip/firewall/filter/remove [find comment="pingota api"]
/interface/wireguard/add name="pingota-wg" listen-port=51820 private-key="{private_key}"
/ip/address/add address="{router.tunnel_address}/{tunnel.prefixlen}" interface="pingota-wg" comment="pingota tunnel"

/interface/wireguard/peers/add interface="pingota-wg" \\
  public-key="{server_public_key()}" \\
  endpoint-address="{settings.ROUTER_TUNNEL_HOST}" endpoint-port="{settings.ROUTER_TUNNEL_PORT}" \\
  allowed-address="{server_ip}/32" persistent-keepalive=25s comment="pingota"

/user/group/add name="pingota" policy="{API_POLICY}"
/user/add name="{router.api_user}" group="pingota" password="{api_password}"
/ip/service/set api address="{server_ip}/32" disabled=no
/ip/firewall/filter/add chain=input action=accept protocol=tcp dst-port=8728 \\
  src-address="{server_ip}" comment="pingota api" place-before=0

:local tunnelReady false
:for attempt from=1 to=10 do={{
  :if ([/ping address="{server_ip}" count=1] > 0) do={{ :set tunnelReady true; :break }}
  :delay 1s
}}
:if ($tunnelReady) do={{
  /tool/fetch url="{report}?version={version}&board={board}" keep-result=no
}} else={{
  :log error "Pingota tunnel did not connect. Check the server address and UDP port."
}}
"""


def report_as_connected(router, version: str = "", board: str = "") -> None:
    """The router called us: it applied the config and is reachable."""
    from apps.network.models import SessionEvent

    router.status = "connected"
    router.installed_at = timezone.now()
    router.last_seen_at = timezone.now()
    router.last_status = "online"
    if version:
        router.routeros_version = version[:40]
    if board:
        router.board_name = board[:80]
    router.save()
    SessionEvent.objects.create(
        isp=router.isp,
        router=router,
        event="connected",
        detail=f"Router installed · RouterOS {router.routeros_version or 'unknown'}",
    )
