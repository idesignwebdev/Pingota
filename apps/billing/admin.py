"""Platform support view of hosting-plan payments.

Guest pass payments stay in the tenant panel; this screen exists so the owner
can confirm a renewal or investigate one without opening the database.
"""
from django.contrib import admin

from .models import PlanPayment


@admin.register(PlanPayment)
class PlanPaymentAdmin(admin.ModelAdmin):
    list_display = ("reference", "isp", "plan", "provider", "amount", "status", "paid_at", "period_end")
    list_filter = ("status", "provider", "plan")
    search_fields = ("reference", "provider_reference")
    readonly_fields = (
        "isp",
        "plan",
        "provider",
        "reference",
        "provider_reference",
        "amount",
        "currency",
        "status",
        "phone",
        "checkout_url",
        "raw_response",
        "failure_reason",
        "period_start",
        "period_end",
        "created_at",
        "paid_at",
    )

    def has_add_permission(self, request):
        return False
