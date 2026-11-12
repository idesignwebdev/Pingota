from django.conf import settings
from django.utils import timezone


def branding(request):
    """Values every template can rely on without a view passing them."""
    isp = getattr(request, "isp", None)
    return {
        "brand_name": "Pingota",
        "brand_tagline": "Hotspot billing and network control",
        "current_isp": isp,
        "brand_color": (isp.brand_color if isp else "#1F6FEB"),
        "year": timezone.now().year,
        "currency": (isp.currency if isp else settings.PAYMENT_DEFAULT_CURRENCY),
    }
