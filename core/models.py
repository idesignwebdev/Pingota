import uuid

from django.db import models


class UUIDModel(models.Model):
    """Primary key that is portable across MongoDB and SQL engines."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True


# Field types that are safe on both backends:
#   CharField, TextField, IntegerField, DecimalField, DateTimeField,
#   BooleanField, JSONField, SlugField, EmailField, URLField.
# Avoid: AutoField (no sequences in Mongo), ArrayField, distinct-on-text,
# and raw SQL — all models go through the ORM.
