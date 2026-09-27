"""Security checker service — validates SSL certificate expirations and performs configuration snapshot tamper detection."""
import datetime
import hashlib
import logging
from pathlib import Path
from cryptography import x509
from cryptography.hazmat.backends import default_backend

from src.services.db import get_config_value, set_config_value, log_security_event

logger = logging.getLogger(__name__)


def parse_cert_info(cert_path: str) -> dict:
    """Parse an SSL certificate to check validity, remaining days, self-signed status, and metadata."""
    path = Path(cert_path)
    if not path.exists():
        return {"valid": False, "error": f"Certificate file not found at {cert_path}"}
        
    try:
        with open(path, "rb") as f:
            cert_data = f.read()
            
        cert = x509.load_pem_x509_certificate(cert_data, default_backend())
        
        # Timezone-aware expiration parsing
        try:
            not_after = cert.not_valid_after_utc
        except AttributeError:
            not_after = cert.not_valid_after.replace(tzinfo=datetime.timezone.utc)
            
        try:
            not_before = cert.not_valid_before_utc
        except AttributeError:
            not_before = cert.not_valid_before.replace(tzinfo=datetime.timezone.utc)

        now = datetime.datetime.now(datetime.timezone.utc)
        days_remaining = (not_after - now).days
        
        # Issuer / Subject RFC4514 strings
        issuer = cert.issuer.rfc4514_string()
        subject = cert.subject.rfc4514_string()
        self_signed = (cert.issuer == cert.subject)

        # Who renews this, so the UI can stop telling everyone to upload a new
        # certificate by hand. A Tailscale-issued certificate carries a public
        # CA and a *.ts.net subject, and is renewed by `tailscale cert` from the
        # host — see docs/remote_access.md.
        managed_by = None
        if not self_signed and ".ts.net" in subject:
            managed_by = "tailscale"
        elif not self_signed:
            managed_by = "external_ca"
        else:
            managed_by = "self_signed"

        return {
            "valid": True,
            "error": None,
            "days_remaining": days_remaining,
            "expires_at": not_after.isoformat(),
            "issued_at": not_before.isoformat(),
            "issuer": issuer,
            "subject": subject,
            "self_signed": self_signed,
            "managed_by": managed_by,
            "auto_renewing": managed_by == "tailscale",
        }
    except Exception as e:
        logger.error(f"Failed to parse SSL certificate at {cert_path}: {e}")
        return {"valid": False, "error": f"Failed to parse certificate: {e}"}


async def compute_security_snapshot_hash() -> str:
    """Compute a unique SHA-256 fingerprint representing all active security files and DB settings."""
    hasher = hashlib.sha256()
    
    # 1. Hash configuration variables from database
    for key in ["security_enabled", "tls_enabled", "mtls_enabled", "tls_cert_path", "tls_key_path", "client_ca_path"]:
        val = await get_config_value(key) or ""
        hasher.update(f"{key}:{val}".encode("utf-8"))
        
    # 2. Hash file contents of cert, key, and client CA files if configured
    cert_path = await get_config_value("tls_cert_path") or "/data/ssl/server.crt"
    key_path = await get_config_value("tls_key_path") or "/data/ssl/server.key"
    ca_path = await get_config_value("client_ca_path") or "/data/ssl/ca.crt"
    
    for path_str in [cert_path, key_path, ca_path]:
        path = Path(path_str)
        if path.exists():
            try:
                with open(path, "rb") as f:
                    file_content = f.read()
                # Feed file metadata + file content hash into global snapshot hasher
                hasher.update(f"file:{path_str}".encode("utf-8"))
                hasher.update(hashlib.sha256(file_content).digest())
            except Exception as e:
                logger.warning(f"Could not read {path_str} during snapshot hash: {e}")
                
    return hasher.hexdigest()


async def run_tamper_check() -> bool:
    """Compare the current security fingerprint against the last recorded DB hash. Logs alerts on mismatch."""
    current_hash = await compute_security_snapshot_hash()
    last_known_hash = await get_config_value("last_security_snapshot_hash")
    
    if last_known_hash is None:
        # Greenfield / first-boot snapshot initialization
        await set_config_value("last_security_snapshot_hash", current_hash)
        await log_security_event("SECURITY_SNAPSHOT_INITIALISED", f"Created initial security fingerprint: {current_hash[:16]}")
        return True
        
    if current_hash != last_known_hash:
        logger.warning(f"⚠️ SECURITY_TAMPER_ALERT: Security snapshot mismatch! Last known: {last_known_hash[:16]}, Current: {current_hash[:16]}")
        await log_security_event(
            "SECURITY_TAMPER_ALERT",
            f"Security settings or cert files modified outside the UI. Last known: {last_known_hash}, Current: {current_hash}",
            "system"
        )
        return False
        
    return True


async def update_security_snapshot() -> None:
    """Call this helper when a user legitimately rotates certificates or alters config via UI."""
    new_hash = await compute_security_snapshot_hash()
    await set_config_value("last_security_snapshot_hash", new_hash)
    await log_security_event("SECURITY_SNAPSHOT_ROTATED", f"Re-keyed snapshot hash to: {new_hash[:16]}")
