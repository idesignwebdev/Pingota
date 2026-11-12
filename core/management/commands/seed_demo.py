"""Demo data so a fresh install shows a working panel instead of empty states.

``--reset`` removes previous demo rows first. Payments created here are marked
with reference prefix ``demo-`` so they are easy to find and delete.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.billing.models import Payment
from apps.accounts.models import Membership
from apps.network.models import AccessGrant, Customer, Package, RouterMetric, SessionEvent, Voucher
from apps.tenants.models import Isp, Router
from core.plans import ensure_subscription_plans

DEMO_PREFIX = "demo-"


class Command(BaseCommand):
    help = "Load demo passes, customers, payments and router metrics."

    def add_arguments(self, parser):
        parser.add_argument("--slug", default="demo-hotspot")
        parser.add_argument("--reset", action="store_true")

    def handle(self, *args, **options):
        ensure_subscription_plans()
        slug = options["slug"]
        if options["reset"]:
            # MongoDB cannot delete through a join across collections, so the
            # related ids are resolved first and each collection is cleared on
            # its own keys.
            demo_isp = Isp.objects.filter(slug=slug).first()
            if demo_isp:
                isp_id = demo_isp.pk
                grants = AccessGrant.objects.filter(isp_id=isp_id)
                customers = Customer.objects.filter(isp_id=isp_id)
                routers = Router.objects.filter(isp_id=isp_id)
                Payment.objects.filter(isp_id=isp_id).delete()
                Voucher.objects.filter(isp_id=isp_id).delete()
                SessionEvent.objects.filter(isp_id=isp_id).delete()
                AccessGrant.objects.filter(pk__in=[g.pk for g in grants]).delete()
                RouterMetric.objects.filter(router__in=[r.pk for r in routers]).delete()
                Customer.objects.filter(pk__in=[c.pk for c in customers]).delete()
                Package.objects.filter(isp_id=isp_id).delete()
                Router.objects.filter(pk__in=[r.pk for r in routers]).delete()
                Membership.objects.filter(isp_id=isp_id).delete()
                demo_isp.delete()
            Payment.objects.filter(reference__startswith=DEMO_PREFIX).delete()
            self.stdout.write("Demo data cleared")

        isp, _ = Isp.objects.get_or_create(
            slug=slug,
            defaults={
                "name": "Kondele Fibre",
                "support_phone": "0712 000 000",
                "currency": "KES",
                "brand_color": "#1F6FEB",
            },
        )

        from apps.network import onboarding

        router = Router.objects.filter(isp=isp, name="Kondele · AP1").first()
        if router is None:
            router = Router(isp=isp, name="Kondele · AP1", hotspot_profile="hsprof1")
            onboarding.provision_identity(router)
            router.status = "connected"
            router.installed_at = timezone.now()
            router.last_seen_at = timezone.now()
            router.last_status = "online"
            router.routeros_version = "7.15.3"
            router.board_name = "hEX S"
            router.save()

        # name, kind, price, minutes, down, up, data cap MB (0 = none)
        packages = [
            ("1 hour", "hourly", 10, 60, 2048, 512, 0),
            ("4 hours", "hourly", 15, 240, 2048, 512, 700),
            ("24 hours", "hourly", 30, 1440, 3072, 768, 2500),
            ("Home monthly", "subscription", 1500, 43200, 8192, 2048, 60000),
        ]
        created = []
        for order, (name, kind, price, minutes, down, up, cap) in enumerate(packages):
            package, _ = Package.objects.update_or_create(
                isp=isp,
                name=name,
                defaults={
                    "kind": kind,
                    "price": Decimal(price),
                    "duration_minutes": minutes,
                    "download_kbps": down,
                    "upload_kbps": up,
                    "data_cap_mb": cap or None,
                    "fup_action": "throttle",
                    "fup_download_kbps": 256,
                    "fup_upload_kbps": 128,
                    "sort_order": order,
                    "is_active": True,
                },
            )
            created.append(package)

        User = get_user_model()
        operator, created_user = User.objects.get_or_create(
            username="operator", defaults={"email": "ops@kondele.example", "is_staff": True}
        )
        if created_user:
            operator.set_password("changeme123")
            operator.save(update_fields=["password"])
        Membership.objects.get_or_create(user=operator, defaults={"isp": isp, "role": "owner"})

        now = timezone.now()
        macs = [
            ("A4:5E:60:1C:92:0B", "254712000111"),
            ("7C:2F:80:44:19:AE", "254733000222"),
            ("F0:18:98:2B:77:C4", "254701000333"),
            ("B8:27:EB:9F:03:71", "254759000444"),
            ("3C:5A:B4:01:22:D9", ""),
        ]
        demo_customers = []
        for index, (mac, phone) in enumerate(macs):
            customer, _ = Customer.objects.get_or_create(
                isp=isp, mac_address=mac, defaults={"phone": phone, "last_seen": now}
            )
            demo_customers.append(customer)
            package = created[index % len(created)]
            started = now - timedelta(hours=index * 3 + 1)
            expires = started + timedelta(minutes=package.duration_minutes)
            AccessGrant.objects.create(
                isp=isp,
                customer=customer,
                package=package,
                router=router,
                state="active",
                started_at=started,
                expires_at=expires,
                hotspot_username="pg-" + mac.replace(":", "").lower(),
            )

        # Payments across the last week so the chart has shape.
        amounts = [10, 15, 30, 30, 10, 15, 30, 1500, 10, 30]
        for day in range(6, -1, -1):
            for slot in range(3 + day % 4):
                package = created[(day + slot) % 3]
                created_at = now - timedelta(days=day, hours=slot + 1)
                Payment.objects.create(
                    isp=isp,
                    package=package,
                    customer=demo_customers[(day + slot) % len(demo_customers)],
                    provider="paystack" if slot % 2 else "mpesa",
                    reference=f"{DEMO_PREFIX}{day}{slot}{package.name[:3].replace(' ', '').lower()}",
                    amount=package.price if package.price else Decimal(amounts[slot]),
                    currency=isp.currency,
                    status="paid",
                    created_at=created_at,
                    paid_at=created_at,
                    provider_reference=f"MP{day}{slot}XYZ",
                )

        # Two unsettled attempts, so the failure state is visible too.
        Payment.objects.create(
            isp=isp,
            package=created[0],
            customer=demo_customers[0],
            provider="mpesa",
            reference=f"{DEMO_PREFIX}pending-1",
            amount=Decimal(10),
            currency=isp.currency,
            status="pending",
        )
        Payment.objects.create(
            isp=isp,
            package=created[2],
            customer=demo_customers[2],
            provider="paystack",
            reference=f"{DEMO_PREFIX}failed-1",
            amount=Decimal(30),
            currency=isp.currency,
            status="failed",
            failure_reason="Declined by issuer",
        )

        for i in range(24):
            RouterMetric.objects.create(
                router=router,
                online_users=[3, 5, 8, 12, 14, 11, 7, 9][i % 8],
                cpu_percent=[8, 12, 19, 24][i % 4],
                reachable=i != 17,
                latency_ms=38 + (i % 5) * 7,
            )

        from apps.network.vouchers import generate as generate_codes

        if not isp.vouchers.exists():
            generate_codes(isp, created[0], 24, batch="Market gate · demo")
            generate_codes(isp, created[2], 12, batch="Market gate · demo")

        SessionEvent.objects.create(isp=isp, grant=None, router=router, event="connected",
                                    detail="A4:5E:60:1C:92:0B · 24 hours")
        SessionEvent.objects.create(isp=isp, router=router, event="expired",
                                    detail="7C:2F:80:44:19:AE · package ended")
        SessionEvent.objects.create(isp=isp, router=router, event="router_error", level="error",
                                    detail="Kondele · AP1 timed out on health probe")

        self.stdout.write(self.style.SUCCESS(
            f"Demo ready. Sign in at /admin/login/ with operator / changeme123. "
            f"Capture page: /portal/{isp.slug}/"
        ))
