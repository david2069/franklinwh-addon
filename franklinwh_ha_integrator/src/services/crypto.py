"""Cryptography service — handles AES-256-GCM database encryption, PBKDF2 derived keys, and native bcrypt password hashing."""
import os
import base64
import logging
from pathlib import Path
import bcrypt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from src.config.environment import get_data_dir

logger = logging.getLogger(__name__)

# Global variables for keys and salts
_SECURITY_KEY_PATH = get_data_dir() / "security.key"
_SALT_FILE_PATH = get_data_dir() / "security.salt"


def hash_password(password: str) -> str:
    """Hash a password utilizing native bcrypt."""
    pw_bytes = password.encode("utf-8")
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(pw_bytes, salt)
    return hashed.decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    """Verify a password against its hash."""
    try:
        pw_bytes = password.encode("utf-8")
        hashed_bytes = hashed.encode("utf-8")
        return bcrypt.checkpw(pw_bytes, hashed_bytes)
    except Exception as e:
        logger.error(f"Password verification failed: {e}")
        return False


def _get_or_create_secret_bytes() -> bytes:
    """Ensure the local security.key exists and retrieve its content."""
    # Ensure data directory exists
    _SECURITY_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)

    if not _SECURITY_KEY_PATH.exists():
        logger.info(f"🔑 Generating fresh local machine security key at {_SECURITY_KEY_PATH}")
        # Generate 32 cryptographically secure random bytes
        key_bytes = os.urandom(32)
        
        # Write to file
        with open(_SECURITY_KEY_PATH, "wb") as f:
            f.write(key_bytes)
            
        # Restrict permissions to owner read/write only (chmod 600)
        try:
            os.chmod(_SECURITY_KEY_PATH, 0o600)
        except Exception as e:
            logger.warning(f"Failed to set chmod 600 on {_SECURITY_KEY_PATH}: {e}")
    else:
        with open(_SECURITY_KEY_PATH, "rb") as f:
            key_bytes = f.read()

    # Integrity guard
    if len(key_bytes) != 32:
        raise ValueError(f"Corrupt machine security key detected at {_SECURITY_KEY_PATH} (must be 32 bytes)")

    return key_bytes


def _get_or_create_salt() -> bytes:
    """Ensure the local security.salt exists and retrieve its content."""
    if not _SALT_FILE_PATH.exists():
        salt = os.urandom(16)
        with open(_SALT_FILE_PATH, "wb") as f:
            f.write(salt)
        try:
            os.chmod(_SALT_FILE_PATH, 0o600)
        except Exception as e:
            logger.warning(f"Failed to set chmod 600 on {_SALT_FILE_PATH}: {e}")
    else:
        with open(_SALT_FILE_PATH, "rb") as f:
            salt = f.read()
            
    if len(salt) != 16:
        raise ValueError(f"Corrupt salt detected at {_SALT_FILE_PATH} (must be 16 bytes)")
        
    return salt


def derive_key(purpose_salt: bytes = b"fhai_db_encryption") -> bytes:
    """Derive an AES-256 key from our machine-bound security key using PBKDF2."""
    secret_bytes = _get_or_create_secret_bytes()
    machine_salt = _get_or_create_salt()
    
    # Combine machine salt and specific purpose salt
    combined_salt = machine_salt + purpose_salt
    
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,  # 256 bits
        salt=combined_salt,
        iterations=100000,
    )
    return kdf.derive(secret_bytes)


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a secret string using AES-256-GCM. Returns a base64 string."""
    if not plaintext:
        return ""
        
    try:
        key = derive_key()
        aesgcm = AESGCM(key)
        
        # 12-byte random nonce
        nonce = os.urandom(12)
        ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
        
        # Pack nonce + ciphertext
        payload = nonce + ciphertext
        return base64.b64encode(payload).decode("utf-8")
    except Exception as e:
        logger.error(f"Encryption failed: {e}")
        raise RuntimeError(f"Encryption failed: {e}")


def decrypt_secret(ciphertext_b64: str) -> str:
    """Decrypt a secret string using AES-256-GCM from a base64 string."""
    if not ciphertext_b64:
        return ""
        
    try:
        payload = base64.b64decode(ciphertext_b64.encode("utf-8"))
        if len(payload) < 12:
            raise ValueError("Ciphertext too short (must be at least 12 bytes nonce)")
            
        nonce = payload[:12]
        ciphertext = payload[12:]
        
        key = derive_key()
        aesgcm = AESGCM(key)
        
        plaintext_bytes = aesgcm.decrypt(nonce, ciphertext, None)
        return plaintext_bytes.decode("utf-8")
    except Exception as e:
        logger.error(f"Decryption failed: {e}")
        raise RuntimeError(f"Decryption failed: {e}")
