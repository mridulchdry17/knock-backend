from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


class CryptoConfigError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = settings.TOKEN_ENCRYPTION_KEY.strip()
    if not key:
        raise CryptoConfigError("TOKEN_ENCRYPTION_KEY is not set")
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except Exception as e:
        raise CryptoConfigError(f"TOKEN_ENCRYPTION_KEY is invalid: {e}") from e


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as e:
        raise CryptoConfigError("token decryption failed (key rotated?)") from e


def encrypt_optional(plaintext: str | None) -> str | None:
    return encrypt(plaintext) if plaintext else None


def decrypt_optional(ciphertext: str | None) -> str | None:
    return decrypt(ciphertext) if ciphertext else None
