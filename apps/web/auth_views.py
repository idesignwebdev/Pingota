"""Sign-in routing for the two kinds of account.

The platform owner is a superuser with no membership and belongs in Django
admin. ISP operators carry a membership and belong in the tenant panel.
"""
from __future__ import annotations

from django.conf import settings
from django.contrib.auth import logout
from django.contrib.auth.views import LoginView
from django.shortcuts import redirect
from django.views import View

from core.security import begin_login, finish_login


class PanelLoginView(LoginView):
    template_name = "admin/login.html"

    def get(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect(self.get_success_url())
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        blocked, account, ip = begin_login(
            request,
            retry_url="/admin/login/",
            retry_label="Back to sign in",
        )
        if blocked is not None:
            return blocked

        response = super().post(request, *args, **kwargs)
        return finish_login(
            request,
            response,
            account,
            ip,
            retry_url="/admin/login/",
            retry_label="Back to sign in",
        )

    def get_success_url(self) -> str:
        user = self.request.user
        membership = getattr(user, "membership", None)
        if user.is_superuser and membership is None:
            # Keep a deep link into Django admin, but never send the owner to
            # the tenant panel where they have no network.
            requested = self.get_redirect_url()
            if requested and requested.startswith("/django-admin/"):
                return requested
            return "/django-admin/"
        return super().get_success_url()


class PanelLogoutView(View):
    """Log out on either POST or GET, redirecting to the login page."""

    def dispatch(self, request, *args, **kwargs):
        logout(request)
        redirect_to = getattr(settings, "LOGOUT_REDIRECT_URL", "/admin/login/")
        return redirect(redirect_to)
