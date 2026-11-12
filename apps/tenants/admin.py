"""Platform administration.

Registered here for support work: approving ISPs, checking a router's onboarding
state, fixing a payment key. Two rules apply throughout:

* an encrypted field is never a form input — it lives in ``readonly_fields`` and
  is rendered through ``masked_secret``, which shows only a fixed-length mask;
* nothing here reveals one ISP's secrets to another, because only the platform
  owner (a superuser with no membership) can reach Django admin at all.
"""
from django.contrib import admin, messages

from .models import Isp, MpesaConfig, PaystackConfig, Router, SubscriptionPlan

admin.site.site_header = "Pingota - platform"
admin.site.site_title = "Pingota platform"
admin.site.index_title = "Tenants and infrastructure"

#: Marker so the admin can tell "empty" from "set but hidden".
MASKED = "•" * 12


@admin.register(Isp)
class IspAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "plan", "trial_ends_at", "status", "routers_count", "created_at")
    list_filter = ("plan", "status")
    search_fields = ("name", "slug", "phone")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("created_at",)

    @admin.display(description="Routers")
    def routers_count(self, obj):
        return obj.routers.count()


@admin.register(Router)
class RouterAdmin(admin.ModelAdmin):
    list_display = ("name", "isp", "tunnel_address", "status", "routeros_version", "last_seen_at")
    list_filter = ("status", "isp")
    search_fields = ("name", "tunnel_address", "board_name")
    # api_password and tunnel_private_key are intentionally absent from every
    # field list: they must never be editable or readable from this screen.
    readonly_fields = (
        "install_token_masked",
        "tunnel_public_key",
        "tunnel_address",
        "routeros_version",
        "board_name",
        "installed_at",
        "last_seen_at",
        "last_status",
        "created_at",
    )
    fieldsets = (
        ("Identity", {"fields": ("isp", "name", "status", "enabled", "hotspot_profile")}),
        ("Tunnel", {"fields": ("tunnel_address", "tunnel_public_key", "host", "port")}),
        ("Access", {"fields": ("api_user", "install_token_masked")}),
        ("Reported", {"fields": ("routeros_version", "board_name", "installed_at", "last_seen_at", "last_status")}),
    )
    actions = ["regenerate_credentials"]

    @admin.display(description="Install token")
    def install_token_masked(self, obj):
        return MASKED if obj.install_token else "not generated"

    @admin.action(description="Regenerate credentials (router must be reinstalled)")
    def regenerate_credentials(self, request, queryset):
        from apps.network.onboarding import provision_identity
        from apps.network.tunguard import TunGuardError

        for router in queryset:
            old_public_key = router.tunnel_public_key
            provision_identity(router)
            try:
                from apps.network.onboarding import register_tunnel_peer, remove_tunnel_peer

                register_tunnel_peer(router)
                router.save()
            except TunGuardError as exc:
                try:
                    remove_tunnel_peer(router.tunnel_public_key)
                except TunGuardError:
                    pass
                self.message_user(request, f"{router.name}: {exc}", messages.ERROR)
                continue
            try:
                remove_tunnel_peer(old_public_key)
            except TunGuardError as exc:
                self.message_user(request, f"{router.name}: old peer cleanup failed: {exc}", messages.WARNING)
        self.message_user(
            request,
            "Credentials regenerated where tunnel setup succeeded. Paste each new install script on its router.",
            messages.WARNING,
        )

    def delete_model(self, request, obj):
        from apps.network.tunguard import TunGuardError

        try:
            from apps.network.onboarding import remove_tunnel_peer

            remove_tunnel_peer(obj.tunnel_public_key)
        except TunGuardError as exc:
            self.message_user(request, f"Tunnel peer cleanup failed for {obj.name}: {exc}", messages.WARNING)
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        from apps.network.tunguard import TunGuardError
        from apps.network.onboarding import remove_tunnel_peer

        for router in queryset.iterator():
            try:
                remove_tunnel_peer(router.tunnel_public_key)
            except TunGuardError as exc:
                self.message_user(request, f"Tunnel peer cleanup failed for {router.name}: {exc}", messages.WARNING)
        super().delete_queryset(request, queryset)

    def has_add_permission(self, request):
        # Routers are created through onboarding, which generates the tunnel and
        # credentials as one unit. A hand-made row would have neither.
        return False


@admin.register(PaystackConfig)
class PaystackConfigAdmin(admin.ModelAdmin):
    list_display = ("isp", "enabled", "public_key", "subaccount_code", "secret_state")
    list_filter = ("enabled",)
    search_fields = ("isp__name", "subaccount_code")
    readonly_fields = ("secret_state",)
    exclude = ("secret_key",)  # encrypted at rest; never a form field

    @admin.display(description="Secret key")
    def secret_state(self, obj):
        return MASKED if obj.secret_key else "not set"


@admin.register(MpesaConfig)
class MpesaConfigAdmin(admin.ModelAdmin):
    list_display = ("isp", "enabled", "shortcode", "secret_state")
    list_filter = ("enabled",)
    search_fields = ("isp__name", "shortcode")
    readonly_fields = ("secret_state",)
    exclude = ("consumer_secret", "passkey")

    @admin.display(description="Secrets")
    def secret_state(self, obj):
        have = [name for name, value in (("secret", obj.consumer_secret), ("passkey", obj.passkey)) if value]
        return f"{MASKED} ({', '.join(have)})" if have else "not set"


@admin.register(SubscriptionPlan)
class SubscriptionPlanAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "monthly_price", "max_routers", "max_active_users")
    prepopulated_fields = {"code": ("name",)}
