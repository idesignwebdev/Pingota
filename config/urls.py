from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path, reverse_lazy
from django.views.generic import RedirectView, TemplateView

from apps.web import marketing_views, onboarding_views, signup_views
from apps.web.auth_views import PanelLoginView

admin.site.site_header = "Pingota"
admin.site.site_title = "Pingota admin"
admin.site.index_title = "Platform administration"

urlpatterns = [
    # Marketing site
    path("", TemplateView.as_view(template_name="marketing/home.html"), name="home"),
    path("pricing/", marketing_views.pricing, name="pricing"),
    path("docs/", TemplateView.as_view(template_name="marketing/docs.html"), name="docs"),
    path("signup/", signup_views.signup, name="signup"),
    path("signup/done/", signup_views.signup_done, name="signup_done"),
    path("signup/<uuid:request_id>/approve/", signup_views.approve, name="signup_approve"),
    # Router call-home after its install script runs (no session, token auth).
    path("routers/<str:token>/report/", onboarding_views.router_report, name="router_report"),
    # Captive portal (served by MikroTik or the hotspot itself)
    path("portal/", include("apps.web.portal_urls")),
    # Operator panel
    path("admin/login/", PanelLoginView.as_view(), name="login"),

    path("admin/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path(
        "admin/password-change/",
        auth_views.PasswordChangeView.as_view(
            template_name="admin/password_change.html",
            success_url=reverse_lazy("password_change_done"),
        ),
        name="password_change",
    ),
    path(
        "admin/password-change/done/",
        auth_views.PasswordChangeDoneView.as_view(template_name="admin/password_change_done.html"),
        name="password_change_done",
    ),
    path("", include("apps.web.admin_urls")),
    path("payments/", include("apps.billing.urls")),
    path("healthz/", TemplateView.as_view(template_name="healthz.txt"), name="healthz"),
    # Browsers still ask for /favicon.ico even when an SVG icon is declared.
    path(
        "favicon.ico",
        RedirectView.as_view(url="/static/favicon.svg", permanent=True),
        name="favicon",
    ),
    # Django's own admin kept for model-level support work.
    path("django-admin/", admin.site.urls),
]
