"""Sign-in routing for the two kinds of account.

The platform owner is a superuser with no membership and belongs in Django
admin. ISP operators carry a membership and belong in the tenant panel.
"""
from __future__ import annotations

from django.contrib.auth.views import LoginView


class PanelLoginView(LoginView):
    template_name = "admin/login.html"

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
