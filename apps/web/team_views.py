"""Owner-only team management for one ISP.

Membership is one per login, so adding a colleague is one user plus one row.
The platform owner is never a member of an ISP: that separation is enforced
here as well as during signup approval.
"""
from __future__ import annotations

import logging
import secrets

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.accounts.models import Membership
from apps.web.admin_views import current_isp, owner_required

log = logging.getLogger(__name__)

ROLE_KEYS = {key for key, _label in Membership.ROLE}


@owner_required
def team(request):
    isp = current_isp(request)
    members = isp.members.select_related("user").order_by("created_at")
    return render(
        request,
        "admin/team.html",
        {
            "active_nav": "team",
            "isp": isp,
            "members": members,
            "roles": Membership.ROLE,
        },
    )


@owner_required
@require_POST
def invite(request):
    """Create a login for this network. The temporary password is shown once."""
    isp = current_isp(request)
    email = (request.POST.get("email") or "").strip().lower()[:254]
    role = request.POST.get("role") or "staff"

    if role not in ROLE_KEYS:
        messages.error(request, "Choose the access level for the new login.")
        return redirect("panel:team")
    try:
        validate_email(email)
    except ValidationError:
        messages.error(request, "Enter a valid email address. It becomes the sign-in name.")
        return redirect("panel:team")

    User = get_user_model()
    existing = User.objects.filter(username=email).first()
    if existing is not None:
        if existing.is_superuser or existing.is_staff:
            messages.error(request, "That email belongs to a platform login. Use a different email.")
            return redirect("panel:team")
        if Membership.objects.filter(user=existing).exists():
            messages.error(request, "That email is already attached to another network.")
            return redirect("panel:team")
        try:
            Membership.objects.create(user=existing, isp=isp, role=role)
        except IntegrityError:
            messages.error(request, "That email was just attached to another network. Use a different email.")
            return redirect("panel:team")
        if not existing.is_active:
            existing.is_active = True
            existing.save(update_fields=["is_active"])
        messages.success(request, f"{email} can now sign in. They already had a password.")
        return redirect("panel:team")

    password = secrets.token_urlsafe(12)
    created = None
    try:
        created = User.objects.create_user(username=email, email=email, password=password)
        Membership.objects.create(user=created, isp=isp, role=role)
    except IntegrityError:
        # MongoDB standalone has no real transaction, so undo the half we made.
        if created is not None:
            created.delete()
        messages.error(request, "That email was just used for another login. Try again.")
        return redirect("panel:team")

    return render(
        request,
        "admin/team_invited.html",
        {"active_nav": "team", "isp": isp, "email": email, "password": password, "role": role},
    )


@owner_required
@require_POST
def set_role(request, member_id):
    isp = current_isp(request)
    member = get_object_or_404(Membership, pk=member_id, isp=isp)
    role = request.POST.get("role") or ""
    if role not in ROLE_KEYS:
        messages.error(request, "Choose a valid access level.")
        return redirect("panel:team")
    if (
        member.role == "owner"
        and role != "owner"
        and isp.members.filter(role="owner").count() == 1
    ):
        messages.error(
            request,
            "This is the only owner. Make another member an owner before changing this one.",
        )
        return redirect("panel:team")
    if member.role != role:
        member.role = role
        member.save(update_fields=["role"])
        messages.success(request, f"{member.user.get_username()} is now {member.get_role_display()}.")
    return redirect("panel:team")


@owner_required
@require_POST
def remove(request, member_id):
    isp = current_isp(request)
    member = get_object_or_404(Membership, pk=member_id, isp=isp)
    if member.user_id == request.user.id:
        messages.error(request, "You cannot remove your own login. Ask another owner.")
        return redirect("panel:team")
    if member.role == "owner" and isp.members.filter(role="owner").count() == 1:
        messages.error(request, "This is the only owner. Make another member an owner first.")
        return redirect("panel:team")

    user = member.user
    username = user.get_username()
    member.delete()
    # Keep the account row for audit, but close the way in.
    user.is_active = False
    user.save(update_fields=["is_active"])
    messages.success(request, f"Removed {username}. Their sign-in no longer works.")
    return redirect("panel:team")
