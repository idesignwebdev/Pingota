"""Canonical hosting plans, created on first boot and by the demo seed.

The marketing pricing page and the router quota both read these rows, so the
price a customer sees and the limit the panel enforces cannot drift apart.
"""
from decimal import Decimal

from apps.tenants.models import SubscriptionPlan

PLAN_SPECS = (
    {
        "code": "starter",
        "name": "Starter",
        "monthly_price": Decimal("500.00"),
        "interval": "monthly",
        "max_routers": 1,
        "max_active_users": 0,
        "features": [
            "Up to 1 router",
            "Unlimited pass and voucher types",
            "Paystack and M-Pesa to your own account",
            "Capture page, operator panel and event log",
            "Support by email, 8am to 5pm",
        ],
    },
    {
        "code": "growth",
        "name": "Growth",
        "monthly_price": Decimal("1000.00"),
        "interval": "monthly",
        "max_routers": 3,
        "max_active_users": 0,
        "features": [
            "Up to 3 routers",
            "Unlimited pass and voucher types",
            "Paystack and M-Pesa to your own account",
            "Capture page, operator panel and event log",
            "Support by email, 8am to 5pm",
        ],
    },
    {
        "code": "scale",
        "name": "Scale",
        "monthly_price": Decimal("2500.00"),
        "interval": "monthly",
        "max_routers": 10,
        "max_active_users": 0,
        "features": [
            "Up to 10 routers",
            "Unlimited pass and voucher types",
            "Paystack and M-Pesa to your own account",
            "Capture page, operator panel and event log",
            "Support by email, 8am to 5pm",
        ],
    },
)


def ensure_subscription_plans() -> list[SubscriptionPlan]:
    """Create any missing plan rows without overwriting an edited price."""
    created = []
    for spec in PLAN_SPECS:
        defaults = {key: value for key, value in spec.items() if key != "code"}
        plan, was_created = SubscriptionPlan.objects.get_or_create(code=spec["code"], defaults=defaults)
        if was_created:
            created.append(plan)
    return created
