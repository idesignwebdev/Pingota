"""Symmetric encryption for secrets stored in the database.

Router passwords, payment keys and install tokens must never be readable from a
database dump, a backup, or the admin UI. They are encrypted with Fernet keyed
from FIELD_ENCRYPTION_KEY.

The key is validated, not merely accepted: a short or guessable passphrase here
would make every other control in this app moot, because anyone holding a DB
dump could brute-force the ciphertext in seconds. Validation runs once at
startup (see ``validate_key`` called from AppConfig.ready) so a bad key fails
the deploy instead of silently weakening every stored secret.

What this protects against: a leaked database or backup.
What it does not: full host compromise, where the env file is readable too.
"""
import base64
import binascii
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class SecretsUnavailable(ImproperlyConfigured):
    """Raised when the encryption key is missing, weak, or wrong."""


#: Minimum length for a passphrase-style key. A real Fernet key (44 chars,
#: base64 of 32 bytes) is well above this.
MIN_KEY_LENGTH = 32

#: Environment names where a weak key is a test convenience, not a leak. Only
#: DEBUG mode tolerates them, and it says so in the log.
_PLACEHOLDER_MARKERS = (
    "change-me",
    "changeme",
    "please-change",
    "your-secret",
    "example",
    "dev-only",
    "test",
    "secret",
)


def _decode_fernet_key(key: str) -> bytes | None:
    """Return raw 32 bytes if ``key`` is already a valid Fernet key, else None."""
    try:
        raw = base64.urlsafe_b64decode(key.encode("utf-8"))
    except (binascii.Error, ValueError):
        return None
    return raw if len(raw) == 32 else None


def validate_key(key: str | None) -> None:
    """Reject keys that would make the stored secrets guessable.

    Accepts either a real Fernet key or a long, non-placeholder passphrase (which
    is stretched with SHA-256). Raises ``SecretsUnavailable`` otherwise.
    """
    if not key:
        raise SecretsUnavailable(
            "FIELD_ENCRYPTION_KEY is not set. Generate one with: "
            "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )

    lowered = key.lower()
    if any(marker in lowered for marker in _PLACEHOLDER_MARKERS) and not settings.DEBUG:
        raise SecretsUnavailable(
            "FIELD_ENCRYPTION_KEY looks like a placeholder. Generate a real key with: "
            "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )

    if _decode_fernet_key(key) is None:
        # A passphrase is acceptable only if it is long and varied enough that
        # offline guessing is not the cheapest attack. Fernet keys bypass this.
        has_variety = (
            any(c.islower() for c in key)
            and any(c.isupper() for c in key)
            and any(c.isdigit() or not c.isalnum() for c in key)
        )
        if len(key) < MIN_KEY_LENGTH or not has_variety:
            raise SecretsUnavailable(
                f"FIELD_ENCRYPTION_KEY must be a 44-character Fernet key, or a passphrase of at "
                f"least {MIN_KEY_LENGTH} characters mixing upper case, lower case and a digit or symbol. "
                "Shorter keys let anyone with a database dump decrypt it offline."
            )


def _key_for(key: str) -> Fernet:
    """Build a Fernet for an already-validated key string."""
    raw = _decode_fernet_key(key)
    if raw is not None:
        # Already a real Fernet key: use it as-is, no stretching applied.
        return Fernet(key.encode("utf-8"))
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _fernet() -> Fernet:
    key = settings.FIELD_ENCRYPTION_KEY
    validate_key(key)
    return _key_for(key)


def encrypt(plain: str) -> str:
    if not plain:
        return ""
    return _fernet().encrypt(plain.encode("utf-8")).decode("utf-8")


def decrypt(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        return ""


def encrypt_with(plain: str, key: str) -> str:
    """Encrypt using an explicit key, for rotation."""
    if not plain:
        return ""
    return _key_for(key).encrypt(plain.encode("utf-8")).decode("utf-8")


def decrypt_with(token: str, key: str) -> str:
    """Decrypt using an explicit key, for rotation and verification."""
    if not token:
        return ""
    try:
        return _key_for(key).decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        return ""


def mask(value: str, visible: int = 0) -> str:
    """Display form: never render a stored secret back to the browser."""
    if not value:
        return "not set"
    if visible <= 0:
        return "•" * 12
    return "•" * max(4, len(value) - visible) + value[-visible:]
