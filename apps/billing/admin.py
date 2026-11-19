"""Platform support view of hosting-plan payments.

Guest pass payments stay in the tenant panel; this screen exists so the owner
can confirm a renewal or investigate one without opening the database.
"""
from decimal import Decimal

from django.contrib import admin
from django.db.models import Sum

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

    # -- MongoDB fix: cross-collection queryset.delete() is not supported ---
    def delete_model(self, request, obj):
        """Delete a single PlanPayment without crossing collections."""
        obj.delete()

    def delete_queryset(self, request, queryset):
        """Delete each object individually so MongoDB never builds a
        cross-collection DELETE query."""
        for obj in queryset:
            obj.delete()

    # -- Revenue summary injected into the changelist header -----------------
    def changelist_view(self, request, extra_context=None):
        # Compute totals across *all* PlanPayments (ignoring current filters)
        # so the admin always sees the full picture.
        all_paid = PlanPayment.objects.filter(status="paid")
        total_revenue = all_paid.aggregate(total=Sum("amount"))["total"] or Decimal("0")
        total_paid_count = all_paid.count()
        total_pending = PlanPayment.objects.filter(status="pending").count()

        extra_context = extra_context or {}
        extra_context.update({
            "total_revenue": total_revenue,
            "total_paid_count": total_paid_count,
            "total_pending": total_pending,
        })
        return super().changelist_view(request, extra_context=extra_context)
