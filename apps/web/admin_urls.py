from django.urls import path

from apps.web import admin_views, onboarding_views, voucher_views

app_name = "panel"

urlpatterns = [
    path("admin/", admin_views.overview, name="overview"),
    path("admin/live/", admin_views.live, name="live"),
    path("admin/network/", admin_views.network, name="network"),
    path("admin/customers/", admin_views.customers, name="customers"),
    path("admin/packages/", admin_views.packages, name="packages"),
    path("admin/packages/add/", admin_views.package_form, name="package_add"),
    path("admin/packages/<uuid:package_id>/", admin_views.package_form, name="package_edit"),
    path("admin/revenue/", admin_views.revenue, name="revenue"),
    path("admin/settings/", admin_views.settings_view, name="settings"),
    # Actions
    path("admin/actions/grant/<uuid:grant_id>/extend/", admin_views.extend_grant, name="extend_grant"),
    path("admin/actions/grant/<uuid:grant_id>/revoke/", admin_views.revoke_grant_view, name="revoke_grant"),
    path("admin/actions/router/<uuid:router_id>/test/", admin_views.test_router, name="test_router"),
    path("admin/actions/device/disconnect/", admin_views.disconnect_device, name="disconnect_device"),
    # Router onboarding
    path("admin/routers/add/", onboarding_views.add_router, name="router_add"),
    path("admin/routers/<uuid:router_id>/", onboarding_views.router_detail, name="router_detail"),
    path("admin/routers/<uuid:router_id>/regenerate/", onboarding_views.regenerate, name="router_regenerate"),
    # Vouchers
    path("admin/vouchers/", voucher_views.list_vouchers, name="vouchers"),
    path("admin/vouchers/batches/", voucher_views.list_batches, name="voucher_batches"),
    path("admin/vouchers/generate/", voucher_views.generate, name="voucher_generate"),
    path("admin/vouchers/<uuid:voucher_id>/void/", voucher_views.void, name="voucher_void"),
    path("admin/vouchers/sheet/", voucher_views.print_sheet, name="voucher_sheet"),
]
