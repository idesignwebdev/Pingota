"""Payment intents.

A Payment is created before the customer is sent to Paystack or M-Pesa. It is
the idempotency anchor: a webhook or callback can be replayed any number of
times and only the first successful transition activates access.
"""
from django.db import models

from core.models import UUIDModel


class Payment(UUIDModel):
    PROVIDERS = [("paystack", "Paystack"), ("mpesa", "M-Pesa")]
    STATUS = [
        ("pending", "Pending"),
        ("paid", "Paid"),
        ("failed", "Failed"),
        ("abandoned", "Abandoned"),
    ]

    isp = models.ForeignKey("tenants.Isp", on_delete=models.CASCADE, related_name="payments")
    package = models.ForeignKey("network.Package", on_delete=models.PROTECT, related_name="payments")
    customer = models.ForeignKey(
        "network.Customer", on_delete=models.SET_NULL, null=True, blank=True, related_name="payments"
    )
    grant = models.ForeignKey(
        "network.AccessGrant", on_delete=models.SET_NULL, null=True, blank=True, related_name="payments"
    )

    provider = models.CharField(max_length=12, choices=PROVIDERS, default="paystack")
    reference = models.CharField(max_length=80, unique=True)
    provider_reference = models.CharField(max_length=120, blank=True, db_index=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=8, default="KES")
    status = models.CharField(max_length=10, choices=STATUS, default="pending", db_index=True)

    phone = models.CharField(max_length=24, blank=True)
    checkout_url = models.URLField(blank=True)
    raw_response = models.JSONField(default=dict, blank=True)
    failure_reason = models.CharField(max_length=200, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.reference} · {self.status}"

    @property
    def is_settled(self) -> bool:
        return self.status == "paid"


class WebhookEvent(UUIDModel):
    """Raw provider deliveries, kept for replay and support."""

    provider = models.CharField(max_length=12)
    event_id = models.CharField(max_length=120, db_index=True)
    payload = models.JSONField(default=dict)
    processed = models.BooleanField(default=False)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-received_at"]

    def __str__(self):
        return f"{self.provider}:{self.event_id}"
