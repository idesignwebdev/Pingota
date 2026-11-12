"""Voucher codes for counter sales and printed sheets.

Codes avoid 0/O/1/I/L so a cashier reading one aloud cannot create a support
call. Redemption is a single conditional update on ``state='unused'``: two people
typing the same code at the same second cannot both get internet.
"""
from __future__ import annotations

import secrets

from django.utils import timezone

from apps.network.models import Voucher
from apps.network.provisioning import ProvisioningDeferred, activate_grant, open_grant

ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


class VoucherError(ValueError):
    """Redeemable problem with a code; message is shown to the guest."""


def new_code(groups: int = 2, size: int = 4) -> str:
    parts = ["".join(secrets.choice(ALPHABET) for _ in range(size)) for _ in range(groups)]
    return "-".join(parts)


def generate(isp, package, count: int, batch: str = "", expires_on=None) -> list[Voucher]:
    """Create ``count`` unused codes with the same pass and print label."""
    count = max(1, min(int(count), 500))
    existing = set(isp.vouchers.values_list("code", flat=True))
    made: list[Voucher] = []
    for _ in range(count):
        for _attempt in range(12):
            code = new_code()
            if code not in existing:
                break
        else:
            raise VoucherError("Could not allocate unique codes; try a smaller batch")
        existing.add(code)
        made.append(
            Voucher.objects.create(
                isp=isp, package=package, code=code, batch=batch[:40], expires_on=expires_on
            )
        )
    return made


def normalise(code: str) -> str:
    cleaned = "".join(ch for ch in (code or "").upper() if ch.isalnum())
    if len(cleaned) != 8:
        raise VoucherError("Voucher codes are 8 characters, for example K7QM-3XPD.")
    return f"{cleaned[:4]}-{cleaned[4:]}"


def redeem(isp, raw_code: str, mac: str, phone: str = ""):
    """Turn a code into an active grant. Returns (voucher, grant)."""
    from apps.network.models import Customer

    code = normalise(raw_code)
    voucher = Voucher.objects.filter(isp=isp, code=code).select_related("package").first()
    if not voucher:
        raise VoucherError("That code is not valid on this network. Check the letters and try again.")
    if voucher.state == "void":
        raise VoucherError("That code was cancelled. Ask the operator for a replacement.")
    if voucher.state == "redeemed":
        raise VoucherError("That code has already been used. Each voucher works once.")
    if voucher.expires_on and voucher.expires_on < timezone.localdate():
        raise VoucherError("That code expired. Ask the operator for a new one.")

    # Conditional update is the lock: only one request can flip unused -> redeemed.
    claimed = Voucher.objects.filter(pk=voucher.pk, state="unused").update(
        state="redeemed", redeemed_at=timezone.now()
    )
    if not claimed:
        raise VoucherError("That code has already been used. Each voucher works once.")

    customer, _ = Customer.objects.get_or_create(isp=isp, mac_address=mac, defaults={"phone": phone})
    grant = open_grant(isp, mac=mac, package=voucher.package, phone=phone)
    voucher.refresh_from_db()
    voucher.redeemed_by = customer
    voucher.grant = grant
    voucher.save(update_fields=["redeemed_by", "grant"])

    try:
        activate_grant(grant, extend=voucher.package.kind == "hourly")
    except ProvisioningDeferred:
        # The code is spent and the grant is queued; the guest is told to wait.
        pass
    return voucher, grant
