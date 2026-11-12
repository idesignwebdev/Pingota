"""App configs for Django's built-in apps.

On MongoDB the admin/auth/contenttypes/sessions tables need ObjectId primary
keys — Mongo has no auto-increment sequences. These subclasses switch the auto
field only when MongoDB is the active backend, so the SQLite/Postgres path keeps
bigint keys and the app's own models keep their UUID keys everywhere.
"""
import os

from django.contrib.admin.apps import AdminConfig
from django.contrib.auth.apps import AuthConfig
from django.contrib.contenttypes.apps import ContentTypesConfig
from django.contrib.sessions.apps import SessionsConfig

MONGO_AUTO_FIELD = "django_mongodb_backend.fields.ObjectIdAutoField"


class MongoAdminConfig(AdminConfig):
    if os.environ.get("MONGODB_URI"):
        default_auto_field = MONGO_AUTO_FIELD


class MongoAuthConfig(AuthConfig):
    if os.environ.get("MONGODB_URI"):
        default_auto_field = MONGO_AUTO_FIELD


class MongoContentTypesConfig(ContentTypesConfig):
    if os.environ.get("MONGODB_URI"):
        default_auto_field = MONGO_AUTO_FIELD


class MongoSessionsConfig(SessionsConfig):
    if os.environ.get("MONGODB_URI"):
        default_auto_field = MONGO_AUTO_FIELD
