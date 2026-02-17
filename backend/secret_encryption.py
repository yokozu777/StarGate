"""
Encryption utilities for Global Secrets Manager.

Uses AES-GCM (Galois/Counter Mode) for authenticated encryption.
Secret material is encrypted at rest using a server key from environment/config.
"""
import os
import base64
import secrets
from pathlib import Path
from typing import Optional
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


class SecretEncryption:
    """
    Handles encryption and decryption of secret material.
    
    Uses AES-GCM with a key derived from server secret (env variable or config).
    """
    
    def __init__(self, server_key: Optional[str] = None):
        """
        Initialize encryption with server key.
        
        Args:
            server_key: Server encryption key. If None, reads from GLOBAL_SECRETS_ENCRYPTION_KEY env var.
                       If env var is not set, generates a key (for development only - not secure for production).
        """
        if server_key is None:
            # This should not happen if get_encryption() is used correctly
            # But keep fallback for direct instantiation
            server_key = os.environ.get('GLOBAL_SECRETS_ENCRYPTION_KEY')
        
        if not server_key:
            # This should not happen if get_encryption() is used correctly
            # But keep fallback for direct instantiation
            import warnings
            warnings.warn(
                "GLOBAL_SECRETS_ENCRYPTION_KEY not set. Using generated key (NOT SECURE for production).",
                UserWarning
            )
            server_key = secrets.token_urlsafe(32)
        
        # Derive encryption key from server key using PBKDF2
        # This ensures consistent key derivation
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,  # AES-256 key length
            salt=b'global_secrets_salt',  # Fixed salt for consistency (in production, consider per-secret salt)
            iterations=100000,
            backend=default_backend()
        )
        self._encryption_key = kdf.derive(server_key.encode('utf-8'))
        self._aesgcm = AESGCM(self._encryption_key)
    
    def encrypt(self, plaintext: str) -> str:
        """
        Encrypt plaintext secret material.
        
        Args:
            plaintext: Secret material to encrypt (e.g., private key, token, password)
            
        Returns:
            Base64-encoded encrypted data with nonce prepended
        """
        if not plaintext:
            return ""
        
        # Generate random nonce (96 bits for AES-GCM)
        nonce = secrets.token_bytes(12)
        
        # Encrypt
        ciphertext = self._aesgcm.encrypt(nonce, plaintext.encode('utf-8'), None)
        
        # Prepend nonce to ciphertext and encode as base64
        encrypted_data = nonce + ciphertext
        return base64.b64encode(encrypted_data).decode('utf-8')
    
    def decrypt(self, encrypted_data: str) -> str:
        """
        Decrypt encrypted secret material.
        
        Args:
            encrypted_data: Base64-encoded encrypted data with nonce prepended
            
        Returns:
            Decrypted plaintext
            
        Raises:
            ValueError: If decryption fails (e.g., wrong key, corrupted data)
        """
        if not encrypted_data:
            return ""
        
        try:
            # Decode base64
            data = base64.b64decode(encrypted_data.encode('utf-8'))
            
            # Extract nonce (first 12 bytes) and ciphertext (rest)
            if len(data) < 12:
                raise ValueError("Encrypted data too short (missing nonce)")
            
            nonce = data[:12]
            ciphertext = data[12:]
            
            # Decrypt
            plaintext = self._aesgcm.decrypt(nonce, ciphertext, None)
            return plaintext.decode('utf-8')
        except Exception as e:
            # Check if GLOBAL_SECRETS_ENCRYPTION_KEY is set
            server_key = os.environ.get('GLOBAL_SECRETS_ENCRYPTION_KEY')
            if not server_key:
                raise ValueError(
                    "Decryption failed: GLOBAL_SECRETS_ENCRYPTION_KEY environment variable is not set. "
                    "The encryption key may have changed after server restart. "
                    "Please set GLOBAL_SECRETS_ENCRYPTION_KEY to the same value used when secrets were created, "
                    "or recreate the secrets with the current key."
                ) from e
            else:
                raise ValueError(
                    f"Decryption failed: The encryption key may have changed, or the secret data is corrupted. "
                    f"Original error: {str(e)}"
                ) from e


# Global instance (initialized on first use)
_encryption_instance: Optional[SecretEncryption] = None


def get_encryption_key_file_path(data_dir: Optional[Path] = None) -> Path:
    """
    Get path to encryption key file.
    
    Args:
        data_dir: Data directory (typically DATA_DIR from app.py, which is BASE_DIR / 'data'). If None, uses current directory.
    
    Returns:
        Path to encryption key file
    """
    if data_dir is None:
        # Try to get from environment or use current directory
        data_dir = Path(os.environ.get('DATA_DIR', os.getcwd()))
    
    key_dir = Path(data_dir) / 'global' / 'secrets'
    key_dir.mkdir(parents=True, exist_ok=True)
    return key_dir / '.encryption_key'


def load_encryption_key(data_dir: Optional[Path] = None) -> Optional[str]:
    """
    Load encryption key from file or environment variable.
    
    Priority:
    1. GLOBAL_SECRETS_ENCRYPTION_KEY environment variable
    2. Saved key file (.encryption_key)
    
    Args:
        data_dir: Data directory (typically DATA_DIR from app.py, which is BASE_DIR / 'data')
    
    Returns:
        Encryption key string, or None if not found
    """
    # First, check environment variable
    env_key = os.environ.get('GLOBAL_SECRETS_ENCRYPTION_KEY')
    if env_key:
        return env_key
    
    # Then, try to load from file
    key_file = get_encryption_key_file_path(data_dir)
    if key_file.exists():
        try:
            with open(key_file, 'r', encoding='utf-8') as f:
                key = f.read().strip()
            if key:
                return key
        except Exception:
            pass
    
    return None


def save_encryption_key(key: str, data_dir: Optional[Path] = None) -> bool:
    """
    Save encryption key to file.
    
    Args:
        key: Encryption key to save
        data_dir: Data directory (typically DATA_DIR from app.py, which is BASE_DIR / 'data')
    
    Returns:
        True if saved successfully, False otherwise
    """
    try:
        key_file = get_encryption_key_file_path(data_dir)
        with open(key_file, 'w', encoding='utf-8') as f:
            f.write(key)
        
        # Set file permissions (owner read/write only)
        os.chmod(key_file, 0o600)
        return True
    except Exception:
        return False


def generate_and_save_encryption_key(data_dir: Optional[Path] = None) -> str:
    """
    Generate a new encryption key and save it to file.
    
    Args:
        data_dir: Data directory (typically DATA_DIR from app.py, which is BASE_DIR / 'data')
    
    Returns:
        Generated encryption key
    """
    key = secrets.token_urlsafe(32)
    save_encryption_key(key, data_dir)
    return key


def get_encryption(data_dir: Optional[Path] = None) -> SecretEncryption:
    """
    Get or create global encryption instance.
    
    Args:
        data_dir: Data directory (typically DATA_DIR from app.py, which is BASE_DIR / 'data')
    
    Returns:
        SecretEncryption instance
    """
    global _encryption_instance
    if _encryption_instance is None:
        # Try to load key from environment, file, or generate new one
        server_key = load_encryption_key(data_dir)
        
        if not server_key:
            # Generate and save new key
            server_key = generate_and_save_encryption_key(data_dir)
            import warnings
            warnings.warn(
                "GLOBAL_SECRETS_ENCRYPTION_KEY not set and no saved key found. "
                "Generated and saved new encryption key. "
                "All existing secrets encrypted with previous key will not be decryptable.",
                UserWarning
            )
        
        _encryption_instance = SecretEncryption(server_key)
    return _encryption_instance

