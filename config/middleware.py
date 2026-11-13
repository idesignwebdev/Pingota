"""Security headers without pulling in a CSP dependency.

A strict Content-Security-Policy would need nonces threaded through every
inline script. Instead the policy names exactly the two third-party origins we
use (Google Fonts) plus 'unsafe-inline' for styles, which the portal needs for
per-tenant brand colours.
"""
from django.conf import settings
from django.shortcuts import redirect


class PlatformAdminGuardMiddleware:
    """Keep ISP operators out of the platform-wide Django admin.

    ``is_staff`` only grants access to the admin site; it does not scope data
    to a tenant. An operator who is staff could read every ISP, so membership
    is the boundary: platform users have no membership.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (
            request.path_info.startswith("/django-admin/")
            and request.user.is_authenticated
            and getattr(request.user, "membership", None) is not None
        ):
            return redirect("/admin/")
        return self.get_response(request)


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if "Content-Security-Policy" not in response:
            style = " ".join(settings.CSP_STYLE_SRC) + " 'unsafe-inline'"
            font = " ".join(settings.CSP_FONT_SRC)
            response["Content-Security-Policy"] = (
                "default-src 'self'; "
                f"style-src {style}; "
                f"font-src {font}; "
                "img-src 'self' data:; "
                "script-src 'self' 'unsafe-inline'; "
                "connect-src 'self'; "
                "frame-ancestors 'self'; "
                "base-uri 'self'; "
                "form-action 'self' https://checkout.paystack.com"
            )
        response.setdefault("Referrer-Policy", "same-origin")
        response.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        return response
