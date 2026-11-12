"""First-run setup, safe to run on every container start.

Creates the operator account from env vars if it does not exist yet, and the
ISP row the panel is bound to. Idempotent: it never overwrites a password or a
name that already exists.
"""
import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils.text import slugify

from apps.tenants.models import Isp
from core.plans import ensure_subscription_plans


class Command(BaseCommand):
    help = "Create the ISP and the first operator account from environment variables."

    def handle(self, *args, **options):
        isp_name = os.environ.get("ISP_NAME", "Demo Hotspot")
        slug = os.environ.get("PORTAL_SLUG") or slugify(isp_name)
        isp, created = Isp.objects.get_or_create(
            slug=slug,
            defaults={"name": isp_name, "support_phone": os.environ.get("ISP_SUPPORT_PHONE", "")},
        )
        self.stdout.write(f"{'Created' if created else 'Found'} ISP {isp.name} ({isp.slug})")

        plans = ensure_subscription_plans()
        if plans:
            self.stdout.write(f"Created {len(plans)} hosting plan(s)")

        username = os.environ.get("ADMIN_USERNAME") or os.environ.get("ADMIN_EMAIL")
        password = os.environ.get("ADMIN_PASSWORD")
        if not username or not password:
            self.stdout.write(
                self.style.WARNING("ADMIN_USERNAME / ADMIN_PASSWORD not set; no operator account created.")
            )
            return

        User = get_user_model()
        if User.objects.filter(username=username).exists():
            self.stdout.write(f"Operator {username} already exists")
        else:
            User.objects.create_superuser(username=username, email=username, password=password)
            self.stdout.write(self.style.SUCCESS(f"Created platform operator {username}"))

        # Platform owner: no membership, so they land on Django admin where
        # signup requests are approved. ISP operators get a Membership instead.
        self.stdout.write(
            f"Portal: /portal/{isp.slug}/  ·  Panel: /admin/  ·  Signups: /django-admin/accounts/signuprequest/"
        )
