"""Authentication backend supporting case-insensitive username or email login."""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.db.models import Q


class CaseInsensitiveEmailOrUsernameBackend(ModelBackend):
    """Authenticate with either username or email, case-insensitively."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None:
            username = kwargs.get("email") or kwargs.get(get_user_model().USERNAME_FIELD)
        if username is None or password is None:
            return None

        user_model = get_user_model()
        # Find user by case-insensitive username or email
        user = (
            user_model._default_manager.filter(
                Q(username__iexact=username) | Q(email__iexact=username)
            )
            .order_by("pk")
            .first()
        )

        if user is None:
            # Run the password hasher once to reduce timing side-channels (#20760)
            user_model().set_password(password)
            return None

        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
