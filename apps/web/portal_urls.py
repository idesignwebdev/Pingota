from django.urls import path

from apps.web import portal_views

app_name = "portal"

urlpatterns = [
    path("", portal_views.index, name="index"),  # uses PORTAL_SLUG
    path("<slug:isp_slug>/", portal_views.index, name="isp_index"),
    path("<slug:isp_slug>/packages/", portal_views.packages, name="packages"),
    path("<slug:isp_slug>/checkout/<uuid:package_id>/", portal_views.checkout, name="checkout"),
    path("<slug:isp_slug>/pay/<uuid:package_id>/", portal_views.checkout, name="checkout_short"),
    path("<slug:isp_slug>/status/<str:reference>/", portal_views.status, name="status"),
    path("<slug:isp_slug>/status/<str:reference>/state/", portal_views.status_state, name="status_state"),
    path("<slug:isp_slug>/pass/", portal_views.my_pass, name="my_pass"),
    path("<slug:isp_slug>/voucher/", portal_views.redeem_voucher, name="redeem_voucher"),
]
