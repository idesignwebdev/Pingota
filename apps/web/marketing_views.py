"""Public marketing pages that read live platform data."""
from __future__ import annotations

from django.shortcuts import render

from apps.tenants.models import SubscriptionPlan


def pricing(request):
    """Prices come from the plan rows the platform owner can edit."""
    plans = list(SubscriptionPlan.objects.all().order_by("monthly_price"))
    lead_plan = next((plan for plan in plans if plan.code == "growth"), plans[0] if plans else None)
    other_plans = [plan for plan in plans if plan.pk != getattr(lead_plan, "pk", None)]
    return render(
        request,
        "marketing/pricing.html",
        {"lead_plan": lead_plan, "other_plans": other_plans},
    )
