"""Packages, subscriptions and device sessions.

Two access models share one billing loop:

* **Hourly** — a prepaid pass: pay KES 30, 24 hours of access. Implemented as a
  hotspot user with `limit-uptime`, removed when the pass expires.
* **Subscription** — a recurring monthly plan: pay KES 1,500, connected for 30
  days. Same router primitive, but duration comes from the plan and renewal is
  what keeps it alive.

Both are enforced by the router, so a customer cannot outlive a pass by
disconnecting, and the server sweeper only has to remove stale entries.
"""
from django.conf import settings
from django.db import models

from core.models import UUIDModel

#: Router addresses are allocated from the shared TunGuard tunnel subnet.
TUNNEL_SUBNET = settings.ROUTER_TUNNEL_SUBNET


class Package(UUIDModel):
    KIND = [("hourly", "Hourly pass"), ("subscription", "Subscription")]

    isp = models.ForeignKey("tenants.Isp", on_delete=models.CASCADE, related_name="packages")
    name = models.CharField(max_length=80)
    kind = models.CharField(max_length=13, choices=KIND, default="hourly")
    price = models.DecimalField(max_digits=10, decimal_places=2)
    duration_minutes = models.IntegerField(
        help_text="Pass length. For subscriptions this is the billing period (e.g. 43200 = 30 days)."
    )
    data_cap_mb = models.IntegerField(null=True, blank=True, help_text="FUP volume; blank means unlimited")
    download_kbps = models.IntegerField(default=0, help_text="0 uses the router profile default")
    upload_kbps = models.IntegerField(default=0)
    device_limit = models.IntegerField(default=1)

    # Fair use policy: what happens when the cap is reached. Throttling keeps
    # the customer online at a slower speed; cutting them off is the strict mode.
    FUP_ACTION = [("throttle", "Throttle to slow profile"), ("block", "Disconnect and require a new package")]
    fup_action = models.CharField(max_length=10, choices=FUP_ACTION, default="throttle")
    fup_download_kbps = models.IntegerField(default=256)
    fup_upload_kbps = models.IntegerField(default=128)
    fup_profile = models.CharField(max_length=60, blank=True, help_text="Router profile to switch to once capped")

    is_active = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "price"]

    def __str__(self):
        return f"{self.name} · {self.price}"

    @property
    def duration_label(self) -> str:
        minutes = self.duration_minutes
        if minutes % 1440 == 0 and minutes >= 1440:
            days = minutes // 1440
            return f"{days} day" + ("s" if days > 1 else "")
        if minutes % 60 == 0:
            hours = minutes // 60
            return f"{hours} hour" + ("s" if hours > 1 else "")
        return f"{minutes} minutes"


class Customer(UUIDModel):
    """A device that has bought access at least once. Keyed by MAC per ISP."""

    isp = models.ForeignKey("tenants.Isp", on_delete=models.CASCADE, related_name="customers")
    mac_address = models.CharField(max_length=17, db_index=True)
    phone = models.CharField(max_length=24, blank=True, db_index=True)
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        unique_together = [("isp", "mac_address")]
        verbose_name_plural = "Customers"

    def __str__(self):
        return self.mac_address


class AccessGrant(UUIDModel):
    """One purchase of access. The unit the router and the sweeper act on."""

    STATE = [("active", "Active"), ("expired", "Expired"), ("revoked", "Revoked")]

    isp = models.ForeignKey("tenants.Isp", on_delete=models.CASCADE, related_name="grants")
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="grants")
    package = models.ForeignKey(Package, on_delete=models.PROTECT, related_name="grants")
    router = models.ForeignKey("tenants.Router", on_delete=models.SET_NULL, null=True, blank=True)
    hotspot_username = models.CharField(max_length=60, blank=True)
    state = models.CharField(max_length=10, choices=STATE, default="active")
    started_at = models.DateTimeField()
    expires_at = models.DateTimeField(db_index=True)
    data_used_mb = models.IntegerField(default=0)
    bytes_synced = models.BigIntegerField(default=0, help_text="Last router counter reading, for FUP deltas")
    fup_state = models.CharField(
        max_length=10,
        choices=[("ok", "Within allowance"), ("throttled", "Throttled"), ("blocked", "Blocked")],
        default="ok",
    )
    revoked_reason = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.customer.mac_address} · {self.package.name}"

    @property
    def seconds_left(self) -> int:
        from django.utils import timezone

        return max(0, int((self.expires_at - timezone.now()).total_seconds()))

    @property
    def is_live(self) -> bool:
        return self.state == "active" and self.seconds_left > 0

    @property
    def time_left_label(self) -> str:
        """Human remaining time; the number the operator scans for."""
        seconds = self.seconds_left
        if seconds <= 0:
            return "ended"
        days, rest = divmod(seconds, 86400)
        hours, rest = divmod(rest, 3600)
        minutes = rest // 60
        if days:
            return f"{days}d {hours:02d}h"
        if hours:
            return f"{hours}h {minutes:02d}m"
        return f"{minutes}m"


class SessionEvent(UUIDModel):
    """Audit trail the dashboard and monitoring views read."""

    LEVELS = [("info", "Info"), ("warn", "Warning"), ("error", "Error")]
    EVENTS = [
        ("connected", "Connected"),
        ("disconnected", "Disconnected"),
        ("expired", "Expired"),
        ("revoked", "Revoked"),
        ("extended", "Extended"),
        ("fup_throttled", "Throttled by allowance"),
        ("fup_blocked", "Blocked by allowance"),
        ("voucher_redeemed", "Voucher redeemed"),
        ("router_error", "Router error"),
        ("payment_error", "Payment error"),
    ]

    isp = models.ForeignKey("tenants.Isp", on_delete=models.CASCADE, related_name="events")
    grant = models.ForeignKey(AccessGrant, on_delete=models.SET_NULL, null=True, blank=True)
    router = models.ForeignKey("tenants.Router", on_delete=models.SET_NULL, null=True, blank=True)
    event = models.CharField(max_length=20, choices=EVENTS)
    level = models.CharField(max_length=6, choices=LEVELS, default="info")
    detail = models.CharField(max_length=400, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]


class RouterMetric(UUIDModel):
    """Small time series for the monitoring charts. One row per sync tick."""

    router = models.ForeignKey("tenants.Router", on_delete=models.CASCADE, related_name="metrics")
    recorded_at = models.DateTimeField(auto_now_add=True, db_index=True)
    online_users = models.IntegerField(default=0)
    cpu_percent = models.IntegerField(default=0)
    reachable = models.BooleanField(default=True)
    latency_ms = models.IntegerField(default=0)

    class Meta:
        ordering = ["-recorded_at"]





class Voucher(UUIDModel):
    """A printed or sold code that grants a pass without a payment.

    Operators print a sheet, sell it at the counter, and the guest enters the
    code on the capture page. The code is redeemed exactly once: redemption sets
    customer and grant, and a second attempt is refused rather than silently
    reusing the same pass.
    """

    STATE = [("unused", "Unused"), ("redeemed", "Redeemed"), ("void", "Void")]

    isp = models.ForeignKey("tenants.Isp", on_delete=models.CASCADE, related_name="vouchers")
    code = models.CharField(max_length=24, db_index=True)
    package = models.ForeignKey(Package, on_delete=models.PROTECT, related_name="vouchers")
    batch = models.CharField(max_length=40, blank=True, help_text="Label such as 'Market gate · Oct'")
    state = models.CharField(max_length=8, choices=STATE, default="unused")
    expires_on = models.DateField(null=True, blank=True, help_text="Unredeemed codes stop working after this date")
    redeemed_by = models.ForeignKey(
        Customer, on_delete=models.SET_NULL, null=True, blank=True, related_name="vouchers_used"
    )
    redeemed_at = models.DateTimeField(null=True, blank=True)
    grant = models.ForeignKey(
        "network.AccessGrant", on_delete=models.SET_NULL, null=True, blank=True, related_name="vouchers"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("isp", "code")]
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.code} · {self.package.name}"
