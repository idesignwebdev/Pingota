"""Create content types and permissions on MongoDB.

Django 5.1 added a ``get_for_models`` hook on ``ContentTypeManager`` so a
backend can override how content types are looked up during ``post_migrate``.
``django-mongodb-backend`` does not implement it (checked on 5.2 and 6.1), so
Django's stock ``create_permissions`` does ``set(ctypes.values())`` on *unsaved*
objects and raises:

    TypeError: Model instances without primary key value are unhashable

The fix is the backend's own documented pattern: insert each content type first
so it has an ObjectId, then hash the saved instances. Connected to
``post_migrate`` before Django's own receiver so permissions exist on first boot.

This only runs on MongoDB. On SQLite/Postgres Django's normal path is untouched.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.auth.management import create_permissions
from django.contrib.contenttypes.models import ContentType
from django.db.models.signals import post_migrate

log = logging.getLogger(__name__)


def create_content_types_and_permissions(sender, using, verbosity=1, **kwargs):
    if not settings.MONGODB_URI:
        return

    # Step 1: insert the content types that are missing, one by one, so every
    # object has a primary key before anything tries to hash it.
    wanted = {
        (app_config.label, model._meta.model_name): model
        for app_config in _apps_for(sender)
        for model in app_config.get_models()
    }
    existing = {
        (ct.app_label, ct.model)
        for ct in ContentType.objects.using(using).all().only("app_label", "model")
    }
    created = 0
    for (app_label, model_name) in wanted:
        if (app_label, model_name) in existing:
            continue
        ContentType.objects.using(using).create(app_label=app_label, model=model_name)
        created += 1

    # Step 2: now that every row has a key, Django's own permission logic can
    # run with objects it can hash.
    create_permissions(app_config=sender, using=using, verbosity=verbosity)
    if verbosity and created:
        log.info("MongoDB: created %s content type(s) before permissions", created)


def _apps_for(sender):
    """Yield the app configs whose models need content types."""
    from django.apps import apps

    if sender is not None and hasattr(sender, "get_models"):
        yield sender
    for config in apps.get_app_configs():
        if config is not sender:
            yield config


def connect():
    post_migrate.connect(
        create_content_types_and_permissions,
        dispatch_uid="core.mongo_permissions.create_content_types_and_permissions",
    )
