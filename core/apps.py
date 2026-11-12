"""App running before the database models, holding shared plumbing.

Models in every app use a UUID primary key (CharField) so the same code and
migrations run on MongoDB and SQL. Mongo has no auto-increment sequences, so
AutoField is avoided everywhere.
"""
from django.apps import AppConfig
from django.conf import settings


class CoreConfig(AppConfig):
    name = "core"
    label = "core"
    verbose_name = "Pingota core"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # Fail the boot on a missing or weak encryption key: every secret in the
        # database depends on it, and discovering that at the first payment is
        # too late.
        from core.crypto import validate_key

        validate_key(settings.FIELD_ENCRYPTION_KEY)

        # On MongoDB, seed content types before Django's permission post_migrate
        # receiver runs (it hashes unsaved objects otherwise).
        from core import mongo_permissions

        mongo_permissions.connect()

        # Start the sweeper in the serving process only; management commands
        # are filtered inside scheduler.start().
        from core import scheduler

        scheduler.start()
