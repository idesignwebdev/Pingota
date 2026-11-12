"""ISP signup from the marketing site.

Collects the request into SignupRequest. No tenant is created here: an operator
approves it, which creates the ISP, its first login and a welcome screen. That
keeps spam and typo'd emails from consuming router tunnel addresses.
"""
from __future__ import annotations

import logging

from django.contrib import messages
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from apps.accounts.models import SignupRequest
from apps.accounts.services import SignupError, approve_signup

log = logging.getLogger(__name__)


@require_http_methods(["GET", "POST"])
def signup(request):
    if request.method == "POST":
        data = request.POST
        required = {
            "business_name": "the network name",
            "email": "an email address",
            "phone": "a phone number",
        }
        missing = [label for field, label in required.items() if not (data.get(field) or "").strip()]
        if missing:
            return render(
                request,
                "marketing/signup.html",
                {"form": data, "error": f"Still needed: {', '.join(missing)}."},
                status=400,
            )

        SignupRequest.objects.create(
            business_name=data["business_name"].strip()[:120],
            contact_name=(data.get("contact_name") or "").strip()[:120],
            email=data["email"].strip()[:254],
            phone=data["phone"].strip()[:24],
            county=(data.get("county") or "").strip()[:60],
            routers=max(1, min(int(data.get("routers") or 1), 500)),
            note=(data.get("note") or "").strip()[:400],
        )
        return redirect("signup_done")

    return render(request, "marketing/signup.html", {})


def signup_done(request):
    return render(request, "marketing/signup_done.html", {})


def approve(request, request_id):
    """Platform-only: turn an approved request into a working ISP account."""
    if not request.user.is_superuser or getattr(request.user, "membership", None) is not None:
        from django.http import Http404

        raise Http404()

    signup = SignupRequest.objects.filter(pk=request_id, status="pending").first()
    if signup is None:
        messages.error(request, "That request is gone or was already handled.")
        return redirect("/django-admin/accounts/signuprequest/")

    try:
        isp, created, password = approve_signup(signup)
    except SignupError as exc:
        messages.error(request, str(exc))
        return redirect("/django-admin/accounts/signuprequest/")

    messages.success(
        request,
        f"{isp.name} created as /portal/{isp.slug}/. "
        + (f"Temporary password for {signup.email}: {password}" if created else f"{signup.email} already had a login."),
    )
    return redirect("/django-admin/accounts/signuprequest/")
