"""Django admin site with the same sign-in protection as the operator panel."""
from __future__ import annotations

from django.contrib.admin import AdminSite

from core.security import begin_login, finish_login


class PingotaAdminSite(AdminSite):
    def login(self, request, extra_context=None):
        if request.method != "POST":
            return super().login(request, extra_context)

        blocked, account, ip = begin_login(
            request,
            scope="django_admin_login",
            limit=10,
            window=60,
            retry_url="/django-admin/login/",
            retry_label="Back to admin sign in",
        )
        if blocked is not None:
            return blocked

        response = super().login(request, extra_context)
        return finish_login(
            request,
            response,
            account,
            ip,
            retry_url="/django-admin/login/",
            retry_label="Back to admin sign in",
        )
