"""Re-encrypt every stored secret under a new FIELD_ENCRYPTION_KEY.

Changing the key without this leaves all router passwords and payment keys
undecryptable. The command decrypts each value with the old key, re-encrypts it
with the new key, and writes it back. Both keys are passed explicitly, so the
command does not depend on which key is currently loaded in the environment.

    # 1. generate a new key
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

    # 2. dry run: prove the old key still opens everything
    python manage.py rotate_key --old-key "$OLD" --new-key "$NEW" --dry-run

    # 3. apply
    python manage.py rotate_key --old-key "$OLD" --new-key "$NEW"

    # 4. put the new key in the environment and restart

Ciphertext that the old key cannot open is reported and left untouched, so a
half-run deploy is visible rather than silent.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.crypto import decrypt_with, encrypt_with, validate_key

#: (model path, [(field, whether it is stored encrypted)]).
#: TunGuard owns the server tunnel key; Pingota only re-encrypts its own stored
#: router and payment credentials.
TARGETS = [
    ("apps.tenants.models.Router", ["api_password", "tunnel_private_key"]),
    ("apps.tenants.models.PaystackConfig", ["secret_key"]),
    ("apps.tenants.models.MpesaConfig", ["consumer_secret", "passkey"]),
]


class Command(BaseCommand):
    help = "Re-encrypt stored secrets from an old key to a new one."

    def add_arguments(self, parser):
        parser.add_argument("--old-key", required=True, help="The FIELD_ENCRYPTION_KEY currently in use")
        parser.add_argument("--new-key", required=True, help="The key to move to")
        parser.add_argument("--dry-run", action="store_true", help="Verify only, write nothing")

    def handle(self, *args, **options):
        old_key = options["old_key"]
        new_key = options["new_key"]

        try:
            validate_key(new_key)
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            raise CommandError(f"New key rejected: {exc}") from exc
        if old_key == new_key:
            raise CommandError("Old and new keys are identical; nothing to do.")

        from django.apps import apps

        total_ok = total_failed = 0
        for model_path, fields in TARGETS:
            # "apps.tenants.models.Router" -> ("tenants", "Router")
            app_path, model_name = model_path.rsplit(".", 1)
            app_label = app_path.rsplit(".", 1)[0].rsplit(".", 1)[-1]
            model = apps.get_model(app_label, model_name)
            label = model._meta.label
            for row in model.objects.all().iterator():
                updates = {}
                for field in fields:
                    token = getattr(row, field, "") or ""
                    if not token:
                        continue
                    plain = decrypt_with(token, old_key)
                    if not plain:
                        total_failed += 1
                        self.stderr.write(
                            f"  {label} {row.pk}.{field}: old key cannot open this value; left as is"
                        )
                        continue
                    updates[field] = plain  # encrypted below with the active key
                    total_ok += 1
                if not updates:
                    continue
                if not options["dry_run"]:
                    # Re-encrypt with the NEW key explicitly. The active key in
                    # the environment is still the old one during this run, so
                    # calling encrypt() here would rewrite the same ciphertext.
                    with transaction.atomic():
                        for field, plain in updates.items():
                            setattr(row, field, encrypt_with(plain, new_key))
                        row.save(update_fields=list(updates))
            self.stdout.write(f"{label}: done")

        verb = "verified" if options["dry_run"] else "re-encrypted"
        self.stdout.write(
            self.style.SUCCESS(f"{verb} {total_ok} value(s); {total_failed} could not be opened")
        )
        if total_failed:
            raise CommandError(
                "Some values were left untouched because the old key could not open them. "
                "Restore the correct FIELD_ENCRYPTION_KEY and run again before deploying."
            )
        if not options["dry_run"]:
            self.stdout.write(
                "Now set FIELD_ENCRYPTION_KEY to the new key in the environment and restart."
            )
