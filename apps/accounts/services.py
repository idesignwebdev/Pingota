"""Turning an approved signup request into a tenant account."""
from __future__ import annotations

import secrets
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from apps.accounts.models import Membership
from apps.tenants.models import Isp


class SignupError(RuntimeError):
    pass


def approve_signup(signup) -> tuple[Isp, bool, str]:
    """Create the ISP, its first login and a 24-hour trial.

    Returns ``(isp, user_was_created, temporary_password)``. The password is
    empty when the email already had a login.
    """
    if signup.status != "pending":
        raise SignupError("That request was already handled.")

    with transaction.atomic():
        base_slug = slugify(signup.business_name)[:60] or f"isp-{secrets.token_hex(3)}"
        slug = base_slug
        suffix = 1
        while Isp.objects.filter(slug=slug).exists():
            suffix += 1
            slug = f"{base_slug[:56]}-{suffix}"

        isp = Isp.objects.create(
            name=signup.business_name,
            slug=slug,
            phone=signup.phone,
            plan="trial",
            trial_ends_at=timezone.now() + timedelta(hours=24),
            support_phone=signup.phone,
        )

        User = get_user_model()
        user, created = User.objects.get_or_create(
            username=signup.email, defaults={"email": signup.email}
        )
        if not created and Membership.objects.filter(user=user).exists():
            raise SignupError(
                f"{signup.email} is already attached to another network. "
                "Use a different email or move the existing account first."
            )

        password = ""
        if created:
            password = secrets.token_urlsafe(12)
            user.set_password(password)
            user.save(update_fields=["password"])
        Membership.objects.create(user=user, isp=isp, role="owner")
        signup.status = "approved"
        signup.save(update_fields=["status"])

    return isp, created, password
