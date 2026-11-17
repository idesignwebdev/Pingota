from django.urls import path

from apps.billing import views

app_name = "payments"

urlpatterns = [
    path("webhook/paystack/", views.paystack_webhook, name="paystack_webhook"),
    path("webhook/paystack/<slug:isp_slug>/", views.paystack_webhook, name="paystack_webhook_isp"),
    path("mpesa/callback/<slug:isp_slug>/", views.mpesa_callback, name="mpesa_callback"),
    path("callback/", views.checkout_return, name="checkout_return"),
    # Hosting-plan payments use the platform account, not a tenant's keys.
    path("plan/webhook/paystack/", views.plan_paystack_webhook, name="plan_paystack_webhook"),
    path(
        "plan/mpesa/callback/<str:reference>/",
        views.plan_mpesa_callback,
        name="plan_mpesa_callback",
    ),
    path("plan/callback/", views.plan_checkout_return, name="plan_checkout_return"),
]
