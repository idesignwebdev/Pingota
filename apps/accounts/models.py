"""Operator accounts.

Every operator belongs to exactly one ISP. A login with no membership can only
reach the platform admin (Django's own admin), never an ISP panel — so adding a
new customer is one row, not a new deployment.
"""
from django.conf import settings
from django.db import models

from core.models import UUIDModel


class Membership(UUIDModel):
    ROLE = [("owner", "Owner"), ("staff", "Staff"), ("readonly", "Read only")]

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="membership")
    isp = models.ForeignKey("tenants.Isp", on_delete=models.CASCADE, related_name="members")
    role = models.CharField(max_length=10, choices=ROLE, default="owner")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} @ {self.isp.slug}"


class SignupRequest(UUIDModel):
    """An ISP asking for an account from the marketing site.

    Kept separate from Isp so spam and unverified emails never create a tenant.
    """

    STATUS = [("pending", "Pending"), ("approved", "Approved"), ("rejected", "Rejected")]

    business_name = models.CharField(max_length=120)
    contact_name = models.CharField(max_length=120, blank=True)
    email = models.EmailField()
    phone = models.CharField(max_length=24)
    county = models.CharField(max_length=60, blank=True)
    routers = models.IntegerField(default=1)
    note = models.CharField(max_length=400, blank=True)
    status = models.CharField(max_length=10, choices=STATUS, default="pending")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.business_name} ({self.email})"
