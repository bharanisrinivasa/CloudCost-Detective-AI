import os
from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured

def get_encryption_key() -> bytes:
    """
    Retrieve the OCI encryption key from environment variable.
    Must be a valid 32-byte urlsafe base64-encoded key.
    Fails closed if the key is missing or invalid.
    """
    key_str = os.environ.get("OCI_ENCRYPTION_KEY")
    if not key_str:
        raise ImproperlyConfigured(
            "OCI_ENCRYPTION_KEY environment variable is not set. OCI connection features are disabled."
        )
    
    try:
        key_bytes = key_str.encode("utf-8")
        # Validate that it is a valid Fernet key
        Fernet(key_bytes)
        return key_bytes
    except Exception as e:
        raise ImproperlyConfigured(
            "OCI_ENCRYPTION_KEY is invalid. It must be a 32-byte url-safe base64-encoded key."
        ) from e

def encrypt_private_key(plaintext_key: str) -> str:
    """
    Encrypt OCI private key using Fernet.
    Returns the encrypted string.
    """
    if not plaintext_key:
        return ""
    key = get_encryption_key()
    f = Fernet(key)
    return f.encrypt(plaintext_key.encode("utf-8")).decode("utf-8")

def decrypt_private_key(encrypted_key: str) -> str:
    """
    Decrypt OCI private key using Fernet.
    Returns the plaintext string.
    """
    if not encrypted_key:
        return ""
    key = get_encryption_key()
    f = Fernet(key)
    return f.decrypt(encrypted_key.encode("utf-8")).decode("utf-8")
