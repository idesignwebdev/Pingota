"""Platform-owner screens for ISP signup requests."""
from django.contrib import admin, messages

from .models import SignupRequest
from .services import SignupError, approve_signup


@admin.register(SignupRequest)
class SignupRequestAdmin(admin.ModelAdmin):
    list_display = ("business_name", "email", "phone", "routers", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("business_name", "email", "phone", "county")
    readonly_fields = ("status", "created_at")
    actions = ["approve_selected"]

    @admin.action(description="Approve selected signup requests")
    def approve_selected(self, request, queryset):
        for signup in queryset.filter(status="pending"):
            try:
                isp, created, password = approve_signup(signup)
            except SignupError as exc:
                self.message_user(
                    request,
                    f"{signup.business_name}: {exc}",
                    messages.ERROR,
                )
                continue
            summary = f"{isp.name} created at /portal/{isp.slug}/."
            if created:
                summary += f" Temporary password for {signup.email}: {password}"
            else:
                summary += f" {signup.email} already had a login."
            self.message_user(request, summary, messages.SUCCESS)
