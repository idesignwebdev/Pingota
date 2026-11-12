from django.urls import path

from apps.billing import views

app_name = "payments"

urlpatterns = [
    path("webhook/paystack/", views.paystack_webhook, name="paystack_webhook"),
    path("webhook/paystack/<slug:isp_slug>/", views.paystack_webhook, name="paystack_webhook_isp"),
    path("mpesa/callback/<slug:isp_slug>/", views.mpesa_callback, name="mpesa_callback"),
    path("callback/", views.checkout_return, name="checkout_return"),
]
