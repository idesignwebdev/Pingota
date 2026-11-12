"""Tenant (ISP) configuration.

Every other model belongs to exactly one ISP. Nothing in the app reads router
or payment credentials from environment variables, which is what allows one
deployment to serve many ISPs safely.
"""
from decimal import Decimal

from django.db import models
from django.utils import timezone

from core.crypto import decrypt, encrypt
from core.models import UUIDModel


class Isp(UUIDModel):
    """An internet service provider operating one or more hotspots."""

    PLAN_CHOICES = [
        ("trial", "Trial"),
        ("starter", "Starter"),
        ("growth", "Growth"),
        ("scale", "Scale"),
    ]
    STATUS_CHOICES = [("active", "Active"), ("suspended", "Suspended")]

    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=60, unique=True)
    phone = models.CharField(max_length=24, blank=True)
    currency = models.CharField(max_length=8, default="KES")
    plan = models.CharField(max_length=16, choices=PLAN_CHOICES, default="trial")
    trial_ends_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="End of the self-service trial. Blank means no trial expiry (legacy and demo networks).",
    )
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="active")

    # Branding used by the captive portal.
    brand_color = models.CharField(max_length=9, default="#1F6FEB")
    support_phone = models.CharField(max_length=24, blank=True)
    terms_url = models.URLField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "ISP"
        verbose_name_plural = "ISPs"

    def __str__(self):
        return self.name

    @property
    def trial_has_ended(self) -> bool:
        """True only for a dated trial.

        Rows created before trials had an end date, and the demo network, keep
        ``trial_ends_at`` empty and stay usable.
        """
        return (
            self.plan == "trial"
            and self.trial_ends_at is not None
            and self.trial_ends_at <= timezone.now()
        )


class Router(UUIDModel):
    """A MikroTik device this ISP owns, plus its onboarding state.

    Onboarding is self-service. Creating a router generates:

    * a tunnel identity (private key stored encrypted, public key registered
      with TunGuard) and a reserved address in the tunnel subnet, so a router
      behind CGNAT needs no port forwarding;
    * a restricted API user and a random password;
    * a single install command the ISP pastes into the router terminal.

    The result is written back by the router itself (``report`` endpoint), so
    the panel only shows "connected" when the router proved it applied the config.
    """

    STATUS = [
        ("pending", "Waiting for install"),
        ("connected", "Connected"),
        ("error", "Last check failed"),
        ("disabled", "Disabled"),
    ]

    isp = models.ForeignKey(Isp, on_delete=models.CASCADE, related_name="routers")
    name = models.CharField(max_length=80)

    # --- credentials -----------------------------------------------------
    host = models.CharField(max_length=120, blank=True, help_text="Tunnel address (generated) or a public IP")
    port = models.IntegerField(default=8728)
    api_user = models.CharField(max_length=60, default="pingota")
    api_password = models.CharField(max_length=512, blank=True)  # encrypted
    hotspot_profile = models.CharField(
        max_length=60,
        default="default",
        help_text="Hotspot user profile that carries the default bandwidth limit",
    )

    # --- onboarding -------------------------------------------------------
    status = models.CharField(max_length=12, choices=STATUS, default="pending")
    install_token = models.CharField(max_length=64, unique=True, blank=True)  # proves a report came from this router
    tunnel_private_key = models.CharField(max_length=120, blank=True)  # encrypted
    tunnel_public_key = models.CharField(max_length=120, blank=True)
    tunnel_address = models.GenericIPAddressField(null=True, blank=True, unique=True)
    routeros_version = models.CharField(max_length=40, blank=True)
    board_name = models.CharField(max_length=80, blank=True)
    installed_at = models.DateTimeField(null=True, blank=True)

    enabled = models.BooleanField(default=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    last_status = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("isp", "name")]
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.host or 'not connected'})"

    def set_password(self, raw: str) -> None:
        self.api_password = encrypt(raw) if raw else ""

    def set_tunnel_private_key(self, raw: str) -> None:
        self.tunnel_private_key = encrypt(raw) if raw else ""

    def get_tunnel_private_key(self) -> str:
        return decrypt(self.tunnel_private_key)

    def get_password(self) -> str:
        return decrypt(self.api_password)

    @property
    def health_state(self) -> str:
        """One health answer for every panel page.

        ``status`` is the onboarding state (waiting, installed, disabled);
        ``last_status`` is what the latest probe returned. Reading both in the
        templates produced contradictory badges, so they are combined here.
        """
        if not self.enabled or self.status == "disabled":
            return "disabled"
        if self.status == "pending":
            return "pending"
        if self.last_status == "online":
            return "online"
        if self.last_status or self.status == "error":
            return "offline"
        return "unchecked"

    @property
    def health_label(self) -> str:
        return {
            "disabled": "Disabled",
            "pending": "Waiting for install",
            "online": "Online",
            "offline": "Last check failed",
            "unchecked": "Not checked",
        }[self.health_state]

    @property
    def health_badge_class(self) -> str:
        return {
            "disabled": "badge--off",
            "pending": "badge--warn",
            "online": "badge--ok",
            "offline": "badge--danger",
            "unchecked": "badge--off",
        }[self.health_state]

    @property
    def health_description(self) -> str:
        if self.health_state == "pending":
            return "Waiting for the install script to run on the router."
        if self.health_state == "disabled":
            return "This router is disabled, so Pingota does not check or use it."
        if self.health_state == "offline":
            return "The last check failed. Open the event log at the bottom for details."
        if self.health_state == "unchecked":
            return "Installed, but health has not been sampled yet."
        details = []
        if self.routeros_version:
            details.append(f"RouterOS {self.routeros_version}")
        if self.board_name:
            details.append(self.board_name)
        return " · ".join(["Connected", *details])


class PaystackConfig(UUIDModel):
    """Paystack merchant credentials, one per ISP.

    An ISP can point at the platform account (leave blank) or bring its own
    subaccount so card and mobile-money settlements reach them directly.
    """

    isp = models.OneToOneField(Isp, on_delete=models.CASCADE, related_name="paystack")
    secret_key = models.CharField(max_length=512, blank=True)  # encrypted
    public_key = models.CharField(max_length=255, blank=True)
    subaccount_code = models.CharField(max_length=60, blank=True)
    enabled = models.BooleanField(default=False)

    def set_secret(self, raw: str) -> None:
        self.secret_key = encrypt(raw) if raw else ""

    def get_secret(self) -> str:
        return decrypt(self.secret_key)


class MpesaConfig(UUIDModel):
    """Safaricom Daraja credentials so payments land in the ISP's own till."""

    isp = models.OneToOneField(Isp, on_delete=models.CASCADE, related_name="mpesa")
    # All four credentials are encrypted: Safaricom treats key+secret as one
    # credential pair, and a shortcode reveals which till an attacker should
    # target.
    shortcode = models.CharField(max_length=512, blank=True)  # encrypted
    consumer_key = models.CharField(max_length=512, blank=True)  # encrypted
    consumer_secret = models.CharField(max_length=512, blank=True)  # encrypted
    passkey = models.CharField(max_length=512, blank=True)  # encrypted
    enabled = models.BooleanField(default=False)

    def set_secret(self, raw: str) -> None:
        self.consumer_secret = encrypt(raw) if raw else ""

    def get_secret(self) -> str:
        return decrypt(self.consumer_secret)

    def set_consumer_key(self, raw: str) -> None:
        self.consumer_key = encrypt(raw) if raw else ""

    def get_consumer_key(self) -> str:
        return decrypt(self.consumer_key)

    def set_shortcode(self, raw: str) -> None:
        self.shortcode = encrypt(raw) if raw else ""

    def get_shortcode(self) -> str:
        return decrypt(self.shortcode)

    def set_passkey(self, raw: str) -> None:
        self.passkey = encrypt(raw) if raw else ""

    def get_passkey(self) -> str:
        return decrypt(self.passkey)


class SubscriptionPlan(UUIDModel):
    """What the ISP pays Pingota for hosting. Billed by Paystack too."""

    INTERVAL = [("monthly", "Monthly"), ("yearly", "Yearly")]

    code = models.SlugField(max_length=40, unique=True)
    name = models.CharField(max_length=60)
    monthly_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    interval = models.CharField(max_length=10, choices=INTERVAL, default="monthly")
    max_routers = models.IntegerField(default=1)
    max_active_users = models.IntegerField(default=0, help_text="0 means unlimited")
    features = models.JSONField(default=list, blank=True)

    def __str__(self):
        return self.name
