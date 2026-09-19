"""App configuration manager — loads from options.json / env vars"""
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from src.config.environment import detect_environment, get_data_dir


@dataclass
class AppConfig:
    # MQTT
    mqtt_enabled: bool = True
    mqtt_host: str = "localhost"
    # Whether mqtt_host was CHOSEN or is still the built-in default. "localhost"
    # is a legitimate choice and an unset value at the same time, so the value
    # alone cannot tell them apart — and an unset one fails as a connection
    # refusal with no explanation of what to set.
    mqtt_host_configured: bool = False
    mqtt_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: str = ""
    mqtt_client_id: str = "franklinwh_bridge"
    mqtt_qos: int = 0
    mqtt_retain_discovery: bool = True
    topic_prefix: str = "franklinwh"
    ha_discovery_prefix: str = "homeassistant"

    # Home Assistant Integration (bootstrap only — runtime values come from DB)
    # ha_enabled / ha_host / ha_token are used at first-boot to seed the DB.
    # After that, the DB values are authoritative. Set these in .env or options.json
    # for headless provisioning; they will be migrated to DB on startup.
    ha_enabled: bool = False
    ha_host: str = ""   # e.g. http://homeassistant:8123
    ha_token: str = ""  # Long-lived HA access token

    # Polling
    poll_interval: int = 30  # seconds

    # Admin UI auth (empty = disabled)
    admin_username: str = ""
    admin_password: str = ""

    # FranklinWH Cloud API (optional — can also be set via admin UI)
    cloud_email: str = ""
    cloud_password: str = ""
    cloud_gateway: str = ""  # default gateway serial (auto-discovered if blank)

    # Logging settings
    log_level: str = "INFO"
    log_max_mb: int = 10
    log_backups: int = 5
    log_filename: str = "franklinwh.log"
    
    # TLS Settings (Standalone baremetal)
    ssl_keyfile: str = ""
    ssl_certfile: str = ""

    # Security Settings (BKL-SEC-01)
    security_enabled: bool = False
    tls_enabled: bool = False
    tls_cert_path: str = "/data/ssl/server.crt"
    tls_key_path: str = "/data/ssl/server.key"
    mtls_enabled: bool = False
    client_ca_path: str = "/data/ssl/ca.crt"
    jwt_secret_key: str = ""
    jwt_expire_minutes: int = 1440

    # Derived
    env: str = "dev"
    data_dir: Path = field(default_factory=lambda: Path("./data"))

    @classmethod
    def load(cls) -> "AppConfig":
        """Load config from options.json (HA Add-on) or environment variables."""
        env = detect_environment()
        data_dir = get_data_dir()
        cfg = cls(env=env, data_dir=data_dir)

        # HA Add-on: read options.json managed by Supervisor
        options_path = data_dir / "options.json"
        if options_path.exists():
            with open(options_path) as f:
                opts = json.load(f)
            cfg.mqtt_enabled = opts.get("mqtt_enabled", cfg.mqtt_enabled)
            if str(opts.get("mqtt_host", "")).strip():
                cfg.mqtt_host = opts["mqtt_host"]
                cfg.mqtt_host_configured = True
            cfg.mqtt_port = int(opts.get("mqtt_port", cfg.mqtt_port))
            cfg.mqtt_username = opts.get("mqtt_username", cfg.mqtt_username)
            cfg.mqtt_password = opts.get("mqtt_password", cfg.mqtt_password)
            cfg.mqtt_client_id = opts.get("mqtt_client_id", cfg.mqtt_client_id)
            cfg.mqtt_qos = int(opts.get("mqtt_qos", cfg.mqtt_qos))
            if "mqtt_retain_discovery" in opts:
                cfg.mqtt_retain_discovery = bool(opts["mqtt_retain_discovery"])
            cfg.topic_prefix = opts.get("topic_prefix", cfg.topic_prefix)
            cfg.ha_discovery_prefix = opts.get("ha_discovery_prefix", cfg.ha_discovery_prefix)
            cfg.poll_interval = int(opts.get("poll_interval", cfg.poll_interval))
            cfg.admin_username = opts.get("admin_username", cfg.admin_username)
            cfg.admin_password = opts.get("admin_password", cfg.admin_password)
            cfg.cloud_email = opts.get("cloud_email", cfg.cloud_email)
            cfg.cloud_password = opts.get("cloud_password", cfg.cloud_password)
            _cg = opts.get("cloud_gateway", cfg.cloud_gateway)
            cfg.cloud_gateway = _cg.upper() if _cg else ""
            cfg.log_level = opts.get("log_level", cfg.log_level)
            cfg.log_max_mb = int(opts.get("log_max_mb", cfg.log_max_mb))
            cfg.log_backups = int(opts.get("log_backups", cfg.log_backups))
            cfg.log_filename = opts.get("log_filename", cfg.log_filename)
            cfg.ssl_keyfile = opts.get("ssl_keyfile", cfg.ssl_keyfile)
            cfg.ssl_certfile = opts.get("ssl_certfile", cfg.ssl_certfile)
            # Security Settings
            if "security_enabled" in opts:
                cfg.security_enabled = bool(opts["security_enabled"])
            if "tls_enabled" in opts:
                cfg.tls_enabled = bool(opts["tls_enabled"])
            cfg.tls_cert_path = opts.get("tls_cert_path", cfg.tls_cert_path)
            cfg.tls_key_path = opts.get("tls_key_path", cfg.tls_key_path)
            if "mtls_enabled" in opts:
                cfg.mtls_enabled = bool(opts["mtls_enabled"])
            cfg.client_ca_path = opts.get("client_ca_path", cfg.client_ca_path)
            cfg.jwt_secret_key = opts.get("jwt_secret_key", cfg.jwt_secret_key)
            if "jwt_expire_minutes" in opts:
                cfg.jwt_expire_minutes = int(opts["jwt_expire_minutes"])
            # HA Integration bootstrap (seeded from options.json on first install)
            if "ha_enabled" in opts:
                cfg.ha_enabled = bool(opts["ha_enabled"])
            cfg.ha_host = opts.get("ha_host", cfg.ha_host)
            cfg.ha_token = opts.get("ha_token", cfg.ha_token)

        # Environment variable overrides (Docker / dev)
        cfg.mqtt_enabled = str(os.environ.get("MQTT_ENABLED", str(cfg.mqtt_enabled))).lower() in ("true", "1", "yes")
        if str(os.environ.get("MQTT_HOST", "")).strip():
            cfg.mqtt_host = os.environ["MQTT_HOST"]
            cfg.mqtt_host_configured = True
        cfg.mqtt_port = int(os.environ.get("MQTT_PORT", cfg.mqtt_port))
        cfg.mqtt_username = os.environ.get("MQTT_USERNAME", cfg.mqtt_username)
        cfg.mqtt_password = os.environ.get("MQTT_PASSWORD", cfg.mqtt_password)
        cfg.mqtt_client_id = os.environ.get("MQTT_CLIENT_ID", cfg.mqtt_client_id)
        if "MQTT_QOS" in os.environ:
            cfg.mqtt_qos = int(os.environ["MQTT_QOS"])
        if "MQTT_RETAIN_DISCOVERY" in os.environ:
            cfg.mqtt_retain_discovery = str(os.environ["MQTT_RETAIN_DISCOVERY"]).lower() in ("true", "1", "yes")
        cfg.admin_username = os.environ.get("ADMIN_USERNAME", cfg.admin_username)
        cfg.admin_password = os.environ.get("ADMIN_PASSWORD", cfg.admin_password)
        cfg.cloud_email = os.environ.get("CLOUD_EMAIL", cfg.cloud_email)
        cfg.cloud_password = os.environ.get("CLOUD_PASSWORD", cfg.cloud_password)
        _cg_env = os.environ.get("CLOUD_GATEWAY", cfg.cloud_gateway)
        cfg.cloud_gateway = _cg_env.upper() if _cg_env else ""
        cfg.log_level = os.environ.get("LOG_LEVEL", cfg.log_level).upper()
        if "LOG_MAX_MB" in os.environ:
            cfg.log_max_mb = int(os.environ["LOG_MAX_MB"])
        if "LOG_BACKUPS" in os.environ:
            cfg.log_backups = int(os.environ["LOG_BACKUPS"])
        cfg.log_filename = os.environ.get("LOG_FILENAME", cfg.log_filename)
        cfg.ssl_keyfile = os.environ.get("SSL_KEYFILE", cfg.ssl_keyfile)
        cfg.ssl_certfile = os.environ.get("SSL_CERTFILE", cfg.ssl_certfile)
        
        # Load Security Settings from Env (BKL-SEC-01)
        if "SECURITY_ENABLED" in os.environ:
            cfg.security_enabled = os.environ["SECURITY_ENABLED"].lower() in ("true", "1", "yes")
        if "TLS_ENABLED" in os.environ:
            cfg.tls_enabled = os.environ["TLS_ENABLED"].lower() in ("true", "1", "yes")
        cfg.tls_cert_path = os.environ.get("TLS_CERT_PATH", cfg.tls_cert_path)
        cfg.tls_key_path = os.environ.get("TLS_KEY_PATH", cfg.tls_key_path)
        if "MTLS_ENABLED" in os.environ:
            cfg.mtls_enabled = os.environ["MTLS_ENABLED"].lower() in ("true", "1", "yes")
        cfg.client_ca_path = os.environ.get("CLIENT_CA_PATH", cfg.client_ca_path)
        cfg.jwt_secret_key = os.environ.get("JWT_SECRET_KEY", cfg.jwt_secret_key)
        if "JWT_EXPIRE_MINUTES" in os.environ:
            cfg.jwt_expire_minutes = int(os.environ["JWT_EXPIRE_MINUTES"])

        # Break-Glass emergency bypass: overrides standard DB/environment security settings
        if os.environ.get("FWH_DISABLE_SECURITY", "").lower() in ("true", "1", "yes"):
            cfg.security_enabled = False
            cfg.tls_enabled = False
            cfg.mtls_enabled = False

        # HA integration bootstrap via env vars (headless provisioning)
        if "HA_ENABLED" in os.environ:
            cfg.ha_enabled = os.environ["HA_ENABLED"].lower() in ("true", "1", "yes")
        cfg.ha_host = os.environ.get("HA_HOST", cfg.ha_host)
        cfg.ha_token = os.environ.get("HA_TOKEN", cfg.ha_token)

        # HA Add-on: set sensible MQTT default
        if env == "ha_addon" and cfg.mqtt_host == "localhost":
            cfg.mqtt_host = "core-mosquitto"

        return cfg

    @property
    def config_hash(self) -> str:
        """Immutable SHA-256 hash of the loaded config for tamper-evidence snapshots."""
        import hashlib
        # We uniquely hash a sorted dictionary representation of the redacted safe configuration
        config_str = json.dumps(self.safe_dict(), sort_keys=True)
        return hashlib.sha256(config_str.encode("utf-8")).hexdigest()

    def safe_dict(self) -> dict:
        """Config dict with secrets redacted — safe to log/return via API."""
        return {
            "env": self.env,
            "data_dir": str(self.data_dir),
            "mqtt_enabled": self.mqtt_enabled,
            "mqtt_host": self.mqtt_host,
            "mqtt_port": self.mqtt_port,
            "mqtt_username": self.mqtt_username,
            "mqtt_password": "***" if self.mqtt_password else "",
            "mqtt_client_id": self.mqtt_client_id,
            "mqtt_qos": self.mqtt_qos,
            "mqtt_retain_discovery": self.mqtt_retain_discovery,
            "topic_prefix": self.topic_prefix,
            "ha_discovery_prefix": self.ha_discovery_prefix,
            "poll_interval": self.poll_interval,
            "cloud_email": self.cloud_email,
            "cloud_password": "***" if self.cloud_password else "",
            "cloud_gateway": self.cloud_gateway,
            "log_level": self.log_level,
            "log_max_mb": self.log_max_mb,
            "log_backups": self.log_backups,
            "log_filename": self.log_filename,
            # Security Settings (JWT key redacted)
            "security_enabled": self.security_enabled,
            "tls_enabled": self.tls_enabled,
            "tls_cert_path": self.tls_cert_path,
            "tls_key_path": self.tls_key_path,
            "mtls_enabled": self.mtls_enabled,
            "client_ca_path": self.client_ca_path,
            "jwt_secret_key": "***" if self.jwt_secret_key else "",
            "jwt_expire_minutes": self.jwt_expire_minutes,
            # HA bootstrap fields (token redacted)
            "ha_enabled": self.ha_enabled,
            "ha_host": self.ha_host,
            "ha_token": "***" if self.ha_token else "",
        }
