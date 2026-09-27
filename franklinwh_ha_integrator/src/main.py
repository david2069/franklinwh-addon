"""
FranklinWH HA Integrator — Application Entry Point

Staged startup:
  0. Detect environment (ha_addon / docker / dev)
  1. Load AppConfig
  2. Init database (create tables)
  3. [Phase 2] Start GatewayService instances (concurrent poll loops)
  4. [Phase 3] Start MQTTPublisher + CommandListener
  5. Mount routes
"""
import asyncio
import logging
import logging.config
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from src import __version__
from src.config.environment import detect_environment
from src.config.manager import AppConfig
from src.services import db

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App state — populated during lifespan startup, read by routes.
# Storage lives in `src.app_state` so route modules can import
# `get_app_state` without re-entering `src.main` mid-load (that
# circular path silently broke Python 3.12 CI from 2026-04 to 2026-08).
# `src.main` re-exports both names for backwards compatibility.
# ---------------------------------------------------------------------------
from src.app_state import app_state, get_app_state  # noqa: F401


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Staged startup / graceful shutdown."""
    # --- Stage 0: Environment ---
    from src.services import log_buffer
    log_buffer.attach()  # capture runtime logs for the Logs UI tab

    env = detect_environment()
    logger.info(f"🚀 FranklinWH HA Integrator v{__version__} starting [{env}]")

    import uuid
    app_state["boot_id"] = uuid.uuid4().hex



    # --- Stage 1: Config ---
    config = AppConfig.load()
    
    # Configure file logging
    import logging.handlers
    root_logger = logging.getLogger()
    log_level = getattr(logging, config.log_level.upper(), logging.INFO)
    root_logger.setLevel(log_level)
    
    if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root_logger.handlers):
        log_file = config.data_dir / config.log_filename
        fh = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=config.log_max_mb * 1024 * 1024,
            backupCount=config.log_backups
        )
        # Predictable format for the Log parser script
        fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
        fh.setLevel(log_level)
        root_logger.addHandler(fh)

    # ── Noisy-library suppression ──────────────────────────────────────────
    # franklinwh_cloud emits PII at DEBUG: JWT logintokens, JSESSIONID
    # cookies, gateway serials, full request/response bodies. Keep it at
    # WARNING by default so production logs stay clean and credentials
    # don't leak. Opt-in to DEBUG by setting FWH_CLOUD_DEBUG=1 (or by
    # running the whole app at LOG_LEVEL=DEBUG — same intent).
    #
    # NOTE: the previous version of this block did the OPPOSITE — it
    # FORCED franklinwh_cloud to DEBUG whenever the app was NOT in DEBUG
    # (the condition was inverted and the comment lied). Fixed 2026-06-17
    # after the user spotted PII in container logs.
    _fwh_cloud_debug = os.environ.get("FWH_CLOUD_DEBUG", "").lower() in ("1", "true", "yes")
    _fwh_cloud_level = logging.DEBUG if (log_level == logging.DEBUG or _fwh_cloud_debug) else logging.WARNING
    for _name in ("franklinwh_cloud", "franklinwh_cloud.auth",
                  "franklinwh_cloud.client", "franklinwh_cloud.mixins",
                  "franklinwh_cloud.mixins.discover"):
        logging.getLogger(_name).setLevel(_fwh_cloud_level)
    # httpx prints every request line at INFO — also overkill. Same
    # logic: WARNING by default, DEBUG only when explicitly requested.
    logging.getLogger("httpx").setLevel(
        logging.DEBUG if log_level == logging.DEBUG else logging.WARNING
    )
    
    # --- Supervisor-supplied settings, resolved in Python ------------------
    #
    # run.sh asks bashio for the timezone and the MQTT broker, but on this
    # install every bashio Supervisor call returns "Unable to access the API,
    # forbidden" while the same token works from Python — /addons/self/info
    # succeeds. Rather than depend on which of the two can reach the Supervisor,
    # fill in anything still missing here, using the path that demonstrably
    # works. Both lookups are no-ops outside the add-on, and neither is fatal.
    #
    # The timezone is resolved, not applied: it is handed to APScheduler, which
    # takes its own, rather than set on the process. Stored timestamps stay UTC
    # — 132 columns default to SQLite's datetime('now'), which is UTC whatever
    # the process says, while 53 Python call sites use naive datetime.now(),
    # which is local. Those agree only on a UTC container, and changing the
    # process timezone makes one column mean two different things.
    #
    # Broker credentials must land before the publisher is constructed.
    try:
        from src.services import supervisor_settings

        # These warn from HERE rather than from run.sh, because here is where
        # the outcome is actually known. run.sh runs first and its bashio calls
        # fail on this install, so warning there announced a broken timezone and
        # a missing broker two seconds before the app quietly fixed both — the
        # first thing a user reads, and untrue.
        _zone = await supervisor_settings.resolve_site_timezone()
        if not _zone:
            logger.warning(
                "Site timezone could not be determined. TOU blocks, demand "
                "windows and export windows are local wall-clock, so schedules "
                "will fire against UTC until Home Assistant reports a zone."
            )

        await supervisor_settings.apply_mqtt_credentials(config)
        # Outside the add-on nothing supplies a broker, and an unset MQTT_HOST
        # silently became "localhost" and a connection refusal.
        await supervisor_settings.report_unconfigured_broker(config, env)
        if env == "ha_addon" and not (config.mqtt_username or "").strip():
            # The add-on path has no equivalent of MQTT_HOST to point at, so
            # report_unconfigured_broker deliberately says nothing here.
            logger.warning(
                "No MQTT credentials from the Supervisor. Connecting to "
                f"{config.mqtt_host} anonymously, which the Mosquitto add-on "
                "refuses by default. Entities are published over MQTT "
                "Discovery, so Home Assistant will see nothing until this "
                "connects — set mqtt_username and mqtt_password in the add-on's "
                "Configuration tab if it does not."
            )
    except Exception as _sup_exc:
        logger.warning(f"Supervisor settings not applied: {_sup_exc!r}")

    logger.info(f"Config: MQTT={config.mqtt_host}:{config.mqtt_port}, poll={config.poll_interval}s, log={config.log_level}")

    # --- Stage 2: Pre-Startup Backup & Integrity ---
    config.data_dir.mkdir(parents=True, exist_ok=True)
    from src.services.backup import BackupManager, DEFAULT_BACKUP_INTERVAL_HOURS
    backup_manager = BackupManager(data_dir=config.data_dir, ttl_days=7, target_hour=2)

    # Crash-safe startup backup — but ONLY if no recent archive exists.
    # On a normal Docker restart the last backup is minutes old; re-running it
    # compresses a 13 MB DB+log zip and blocks startup for ~4 minutes.
    # We skip it if the newest archive is younger than the backup interval (6h default).
    _backup_dir = config.data_dir / "backups"
    _archives = sorted(_backup_dir.glob("backup_*.zip"), key=lambda p: p.stat().st_mtime, reverse=True) if _backup_dir.exists() else []
    _interval_secs = DEFAULT_BACKUP_INTERVAL_HOURS * 3600
    _needs_backup = True
    if _archives:
        import time as _time
        _age_secs = _time.time() - _archives[0].stat().st_mtime
        if _age_secs < _interval_secs:
            _needs_backup = False
            logger.info(
                f"Startup backup skipped — newest archive is {int(_age_secs / 60)}m old "
                f"(threshold: {DEFAULT_BACKUP_INTERVAL_HOURS}h). Scheduled backup unchanged."
            )

    if _needs_backup:
        logger.info("Taking crash-safe startup backup before touching SQLite…")
        await backup_manager.execute_backup()

    backup_manager.start()

    # --- Stage 2.5: Config File Integrity Checks + Secret Migration ---
    from src.services.config_integrity import run_startup_integrity_checks
    env_path = Path(".") / ".env"  # project root
    integrity_results = await run_startup_integrity_checks(config.data_dir, env_path)
    if integrity_results.get("env", {}).get("status") == "changed":
        logger.warning("⚠️  .env was modified since last boot — review change log")
    if integrity_results.get("options", {}).get("status") == "changed":
        logger.warning("⚠️  options.json was modified since last boot")

    # --- Stage 3: Database ---
    db_path = config.data_dir / "config.db"
    await db.init_db(db_path)

    # --- Stage 3.2: Tamper & Integrity Check ---
    from src.services.security_checker import run_tamper_check
    try:
        clean = await run_tamper_check()
        if not clean:
            logger.warning("🚨 SECURITY_TAMPER_ALERT: Security snapshot mismatch! Configuration files or certs have been modified since last shutdown.")
        else:
            logger.info("🔒 Security Integrity check: CLEAN")
    except Exception as e:
        logger.error(f"Failed to run security tamper check: {e}")

    # --- Stage 3.1: Seed Device Registry catalog (idempotent, INSERT OR IGNORE) ---
    _seed_path = Path(__file__).parent.parent / "db" / "seed" / "device_catalog_seed.json"
    if _seed_path.exists():
        await db.seed_device_catalog(_seed_path)
    else:
        logger.warning(f"Device catalog seed file not found at {_seed_path} — registry will be empty until seeded")

    all_gws = await db.get_all_gateways()
    if not all_gws:
        await db.log_startup(env, phase=0, details={"action": "INITIAL_GREENFIELD_BOOT", "config_hash": config.config_hash})
        logger.info(f"Greenfield Boot detected (0 gateways). Config Hash: {config.config_hash}")
        
        # Headless provisioning check
        if config.cloud_email and config.cloud_password:
            logger.info("Headless Setup: Credentials detected in environment. Initiating Cloud discovery...")
            from src.routes.api_gateways import _discover_gateways, add_gateway, GatewayAddRequest
            try:
                res = await _discover_gateways(config.cloud_email, config.cloud_password)
                if res.get("ok") and res.get("gateways"):
                    discovered = res["gateways"]

                    # cloud_gateway is an optional filter, not a picker. It was
                    # matched against one gateway and the rest silently dropped,
                    # so a two-gateway account provisioned one and looked like it
                    # had worked. Blank means every gateway on the account;
                    # a comma-separated list selects several.
                    wanted = [w.strip() for w in str(config.cloud_gateway or "").split(",") if w.strip()]
                    if wanted:
                        targets = [g for g in discovered
                                   if any(g["serial"].endswith(w) for w in wanted)]
                        missing = [w for w in wanted
                                   if not any(g["serial"].endswith(w) for g in discovered)]
                        if missing:
                            logger.warning(
                                f"Headless Setup: no gateway on this account matches {missing} — "
                                f"available: {[g['serial'][-8:] for g in discovered]}"
                            )
                    else:
                        targets = discovered

                    provisioned = 0
                    for gw_match in targets:
                        try:
                            logger.info(f"Headless Setup: registering {gw_match['serial']}…")
                            await add_gateway(GatewayAddRequest(
                                full_serial=gw_match["serial"],
                                name=gw_match["name"],
                                site_id=gw_match["site_id"],
                                email=config.cloud_email,
                                password=config.cloud_password,
                            ))
                            provisioned += 1
                        except Exception as _e:
                            # One bad gateway must not abandon the rest.
                            logger.error(f"Headless Setup: {gw_match['serial']} failed: {_e}")

                    if provisioned:
                        # Without this the first-run wizard opens on a install
                        # that provisioned itself — asking for credentials it
                        # was already given.
                        await db.set_config_value("setup_complete", "true")
                        logger.info(
                            f"Headless Setup: provisioned {provisioned} of {len(targets)} gateway(s); "
                            "setup marked complete"
                        )
                        all_gws = await db.get_all_gateways()
                    else:
                        logger.warning("Headless Setup: no gateway could be registered")
                else:
                    logger.warning(f"Headless Setup Failed: {res.get('error', 'No gateways found under account')}")
            except Exception as e:
                logger.error(f"Headless Setup Error: {e}")
    else:
        await db.log_startup(env, phase=2, details=config.safe_dict())

    logger.info(f"Database ready at {db_path}")
    migrated = await db.migrate_credentials_from_json()
    if migrated:
        logger.info(f"Migrated credentials for {migrated} gateway(s) from legacy field")

    # --- Stage 3.5: Migrate secrets from env vars → DB (one-time, non-destructive) ---
    # Bootstrap: if env vars carry secrets that haven't yet been written to the DB,
    # write them now. This enables headless provisioning via .env / options.json on
    # first install, then the DB becomes the authoritative store.
    _secrets_migrated = []
    if config.ha_token and not await db.get_config_value("ha_token"):
        await db.set_config_value("ha_token", config.ha_token)
        _secrets_migrated.append("HA_TOKEN")
    if config.ha_host and not await db.get_config_value("ha_host"):
        await db.set_config_value("ha_host", config.ha_host)
        _secrets_migrated.append("HA_HOST")
    if config.mqtt_password and not await db.get_config_value("mqtt_password_db"):
        await db.set_config_value("mqtt_password_db", config.mqtt_password)
        _secrets_migrated.append("MQTT_PASSWORD")
    if _secrets_migrated:
        logger.info(
            f"🔐 Secrets migrated from env/options to DB: {', '.join(_secrets_migrated)}. "
            "You can now remove these values from .env to reduce plaintext exposure."
        )
    if config.ha_enabled and not await db.get_config_value("ha_enabled"):
        from datetime import datetime, timezone
        await db.set_config_value("ha_enabled", True)
        await db.set_config_value("ha_started_at", datetime.now(timezone.utc).isoformat())
        logger.info("HA integration auto-enabled from env/options.json configuration")

    # --- Stage 3: MQTT Publisher + Listener ---
    from src.services.mqtt_publisher import MQTTPublisher
    from src.services.mqtt_listener import CommandListener

    # Determine MQTT enabled state — DB-driven, with env/options.json as bootstrap.
    # First install: mqtt_enabled DB key is absent → default to False (opt-in).
    # Subsequent boot: honour whatever was set in DB (remembered preference).
    # Env var MQTT_ENABLED still overrides if explicitly set.
    mqtt_enabled_db = await db.get_config_value("mqtt_enabled", None)
    if mqtt_enabled_db is None:
        # First install — apply the opted-in default from config
        mqtt_enabled = config.mqtt_enabled  # False by default unless env/options set it
        if mqtt_enabled:
            # Record in DB so subsequent boots use DB value
            await db.set_config_value("mqtt_enabled", True)
            logger.info("MQTT auto-enabled from env/options.json on first install")
        else:
            logger.info("First install: MQTT disabled by default. Enable via MQTT Admin → Configuration tab.")
    else:
        # Remembered preference — honour DB, but env var can still override
        mqtt_enabled = bool(mqtt_enabled_db)
        if "MQTT_ENABLED" in __import__("os").environ:
            mqtt_enabled = config.mqtt_enabled  # explicit env override

    publisher = None
    listener = None
    registry = None
    automation_engine = None

    # Are we fully configured?
    is_setup_complete = len(all_gws) > 0

    # --- Stage 3.6: Seed mqtt_entity_prefix into DB on first boot ---
    # This makes the DB the single authoritative source for the entity prefix,
    # so the migration tool can always read the "old" prefix before changing it.
    # Safe: on subsequent boots the stored value is returned unchanged.
    _DEFAULT_ENTITY_PREFIX = "franklinwh_{short_id}_"
    entity_prefix_template = await db.get_config_value("mqtt_entity_prefix", None)
    if entity_prefix_template is None:
        entity_prefix_template = _DEFAULT_ENTITY_PREFIX
        await db.set_config_value("mqtt_entity_prefix", entity_prefix_template)
        logger.info(f"mqtt_entity_prefix seeded in DB: {entity_prefix_template!r}")
    else:
        logger.info(f"mqtt_entity_prefix loaded from DB: {entity_prefix_template!r}")

    # Device name template, alongside the entity prefix and for the same reason:
    # this is the user's choice, not a format decided in code. It changed three
    # times in one release cycle while it was hardcoded, and every change churns
    # the device in Home Assistant. Default matches FEM and the earlier
    # integrations — "FranklinWH aGate X-01-AU 0091".
    _DEFAULT_DEVICE_NAME = "FranklinWH {model} {serial4}"
    device_name_template = await db.get_config_value("mqtt_device_name", None)
    if device_name_template is None:
        device_name_template = _DEFAULT_DEVICE_NAME
        await db.set_config_value("mqtt_device_name", device_name_template)

    # Slug aliases — the other half of the prefix mechanism. The prefix has been
    # templated and migratable for a long time while the slug stayed hardcoded
    # in models/entities.py, so an install could match an older integration's
    # prefix and still miss on a measurement that integration named differently,
    # with nothing here able to fix it.
    slug_aliases = await db.get_config_value("mqtt_slug_aliases", None)
    if not isinstance(slug_aliases, dict):
        slug_aliases = {}
    if slug_aliases:
        logger.info(f"mqtt_slug_aliases loaded: {slug_aliases}")

    if mqtt_enabled:
        publisher = MQTTPublisher(
            host=config.mqtt_host,
            port=config.mqtt_port,
            username=config.mqtt_username,
            password=config.mqtt_password,
            client_id=config.mqtt_client_id,
            qos=config.mqtt_qos,
            retain_discovery=config.mqtt_retain_discovery,
            topic_prefix=config.topic_prefix,
            discovery_prefix=config.ha_discovery_prefix,
            entity_prefix_template=entity_prefix_template,
            device_name_template=device_name_template,
            slug_aliases=slug_aliases,
        )

        listener = CommandListener(
            host=config.mqtt_host,
            port=config.mqtt_port,
            username=config.mqtt_username,
            password=config.mqtt_password,
            client_id=f"{config.mqtt_client_id}_listener",
            topic_prefix=config.topic_prefix,
        )

        if is_setup_complete:
            publisher.start()
            listener.start()
        else:
            logger.info("MQTTPublisher and CommandListener SUSPENDED: no gateway registered yet — the setup wizard opens on first visit.")
    else:
        logger.info("MQTT disabled: running purely in standalone web mode")

    # --- Stage 4: Gateway Registry (with optional MQTT fan-out) ---
    from src.services.gateway_registry import GatewayRegistry

    async def on_gateway_data(full_serial: str, data: dict) -> None:
        """Fan-out callback: publish state + update availability on each poll (if MQTT enabled)."""
        if publisher:
            publisher.publish_state(full_serial, data)
            publisher.publish_availability(full_serial, online=True)
            # Publish forecast loads states for the gateway
            try:
                short_id = full_serial[-8:]
                await publisher.publish_forecast_loads_state(short_id)
            except Exception as e:
                logger.error(f"Failed to publish forecast loads states: {e}")

    registry = GatewayRegistry(
        poll_interval=config.poll_interval,
        on_data=on_gateway_data,
    )
    if listener:
        listener.registry = registry
        
    if is_setup_complete:
        started = await registry.start_all()
        logger.info(f"Gateway registry: {started} gateway(s) started")
    else:
        logger.info("GatewayRegistry SUSPENDED: no gateway registered yet — the setup wizard opens on first visit.")

    # --- Stage 5: Background BMS History Poller ---
    from src.services.bms_history import bms_history_manager
    if is_setup_complete:
        bms_history_manager.start()
    else:
        logger.info("BMS History Poller SUSPENDED.")

    # --- Stage 6: The Automation Engine (APScheduler) ---
    from src.services.scheduler_core import init_engine
    automation_engine = init_engine(str(db_path), registry)
    if is_setup_complete:
        automation_engine.start()
        # Phase 2.A (2026-08-05) — register SmartDispatch temporal-loop
        # jobs (Macro today; Meso/Micro added in later Phase 2 stages).
        try:
            automation_engine.register_sd_jobs()
        except Exception as _sd_reg_exc:
            logger.warning(f"SmartDispatch scheduler jobs failed to register (non-fatal): {_sd_reg_exc}")
    else:
        logger.info("Scheduler Automation Engine SUSPENDED.")

    # --- Stage 7: Pricing Service ---
    from src.services.pricing.service import pricing_registry
    await pricing_registry.start_all()
    logger.info("PricingRegistry: started all enabled utility services")

    # --- Stage 8: Schedule Presets ---
    from src.services.schedule_presets import SchedulePresets
    try:
        schedule_presets = SchedulePresets()
        schedule_presets.seed_defaults()
        logger.info("📋 Schedule presets initialised")
    except Exception as e:
        logger.warning(f"📋 Schedule presets init failed (non-fatal): {e}")
        schedule_presets = None

    app_state.update({
        "env": env,
        "version": __version__,
        "config": config,
        "registry": registry,
        "publisher": publisher,
        "listener": listener,
        "backup_manager": backup_manager,
        "scheduler": automation_engine,
        "pricing_registry": pricing_registry,
        "schedule_presets": schedule_presets,
        "db_path": str(db_path),
        "setup_required": not is_setup_complete
    })

    # --- Stage 9: Smart Dispatch (Automation Rulebook + Preset Templates) ---
    from src.services.smart_dispatch import smart_dispatch_engine
    try:
        await db.seed_default_rulebook()
        await db.seed_automation_builder_defaults()
        logger.info("SmartDispatch: automation rulebook seeded/verified")
    except Exception as _sd_exc:
        logger.warning(f"SmartDispatch: rulebook seed failed (non-fatal): {_sd_exc}")

    if schedule_presets:
        try:
            await db.migrate_amber_presets_to_generic(schedule_presets)
        except Exception as _mig_exc:
            logger.warning(f"SmartDispatch: Presets/Rules migration failed (non-fatal): {_mig_exc}")

        try:
            _presets_seeded = await db.seed_smart_dispatch_tou_templates(schedule_presets)
            if _presets_seeded:
                logger.info(f"SmartDispatch: seeded {_presets_seeded} Smart TOU preset template(s)")
        except Exception as _tpl_exc:
            logger.warning(f"SmartDispatch: TOU template seed failed (non-fatal): {_tpl_exc}")
        smart_dispatch_engine.set_presets_manager(schedule_presets)
        smart_dispatch_engine.set_gateway_registry(registry)
        logger.info("SmartDispatch: engine initialised with presets manager + gateway registry")
    else:
        logger.info("SmartDispatch: running in signal-only mode (no presets manager)")

    # --- Stage 9b: Persona silent-migration hook (v0.6.0, GH #11) ---
    # First-run detection: if persona.detected_at is null, run detect once
    # in the background so existing installs get baseline persona flags
    # without wizard interruption. Errors non-fatal — persona defaults are
    # conservative (nothing hidden that wasn't hidden before).
    if is_setup_complete:
        async def _persona_first_run_detect():
            from src.services import db as _db, persona_detector
            try:
                if await _db.get_config_value("persona.detected_at", None) is None:
                    logger.info("Persona: first-run detection triggered (silent)")
                    result = await persona_detector.detect_and_persist(force=True)
                    logger.info(f"Persona: silent detect complete — {len(result.get('per_gateway', {}))} gateway(s) profiled")
            except Exception as _p_exc:
                logger.warning(f"Persona silent detection failed (non-fatal): {_p_exc}")
        asyncio.create_task(_persona_first_run_detect(), name="persona_first_run")

    # --- Site names for gateways registered before they were stored ---------
    # Registration kept only the site id, so the dashboard header printed
    # "Site 3447" directly above "SITE ID: 3447". New registrations carry the
    # name; older rows have nothing, and the names live in the cloud where a
    # migration cannot reach them. Backgrounded and best-effort: it makes one
    # discovery call, only while a name is actually missing, and a failure
    # leaves the id showing exactly as before.
    async def _backfill_site_names():
        try:
            from src.routes.api_gateways import backfill_site_names
            await backfill_site_names()
        except Exception as _s_exc:
            logger.debug(f"Site name backfill skipped: {_s_exc!r}")

    asyncio.create_task(_backfill_site_names(), name="site_name_backfill")

    # --- Repair model names built from a hardware version -------------------
    # Discovery used to write "aGate 102" — 102 is sysHdVersion, an integer,
    # shown to the user as a product name. The derivation was fixed, but
    # gateways.model is only written at registration, so every install that
    # registered before the fix kept the fabricated name on screen. Fixing the
    # derivation did nothing for anyone who already had the wrong value.
    async def _repair_gateway_models():
        try:
            from src.services.model_repair import repair_gateway_models
            await repair_gateway_models()
        except Exception as _m_exc:
            logger.debug(f"Model repair skipped: {_m_exc!r}")

    asyncio.create_task(_repair_gateway_models(), name="model_repair")

    # --- Home Assistant configures itself under the Supervisor --------------
    # There is nothing for the user to enter: we already hold a token and
    # already know where Home Assistant is. Without this, notification routing
    # had no parent instance to hang a device off, so the Companion Devices
    # table could not be filled in even by hand, while the HA panel reported
    # "Host Address: Not Set" about a connection that was working.
    async def _ha_autoconfigure():
        try:
            from src.services import ha_autoconfig
            await ha_autoconfig.run()
        except Exception as _h_exc:
            logger.debug(f"HA auto-configuration skipped: {_h_exc!r}")

    asyncio.create_task(_ha_autoconfigure(), name="ha_autoconfigure")

    # --- Remove the devices a changed identifier left behind ----------------
    # Home Assistant keys a device on `device.identifiers`. Ours changed from
    # franklinwh_{short_id} to franklinwh_{full_serial}, which declares a new
    # device rather than renaming the old one — and because discovery messages
    # are retained, HA rebuilt the old device, with every entity it ever had,
    # on every restart. Nothing overwrote those configs, so this cannot heal on
    # its own; and finding it meant knowing to look in MQTT Admin.
    #
    # Only our own retired identity is swept: franklinwh_<short_id> for a
    # gateway that is still registered here. Anything else that looks orphaned
    # is reported, not deleted — it may be a gateway the user removed on
    # purpose, and guessing would destroy entity history.
    async def _sweep_ghost_devices():
        try:
            if str(await db.get_config_value("mqtt_auto_remove_ghosts", "1")) != "1":
                return
            if not publisher or not is_setup_complete:
                return

            from src.services import retained_cleanup

            # The publisher needs to be connected before a scan means anything.
            for _ in range(30):
                if getattr(publisher, "connected", False):
                    break
                await asyncio.sleep(2)
            else:
                logger.debug("ghost sweep skipped: MQTT publisher never connected")
                return

            gateways = await db.get_all_gateways() or []
            serials = {str(g.get("full_serial") or "").strip() for g in gateways}
            shorts = {str(g.get("short_id") or "").strip() for g in gateways}
            live = {f"franklinwh_{s}" for s in serials if s}
            if not live:
                return

            retired = retained_cleanup.retired_identities(shorts, live)
            discovery_prefix = getattr(publisher, "discovery_prefix", "homeassistant") or "homeassistant"
            result = await retained_cleanup.auto_sweep(publisher, discovery_prefix, live, retired)

            for dev in result["removed"]:
                logger.info(
                    "ghost sweep: removed old device %r (%s) — %d entity config(s) cleared",
                    dev["device_name"], dev["identity"], dev["entity_count"],
                )
            for dev in result["left"]:
                logger.info(
                    "ghost sweep: left %r (%s) in place — %d entity config(s); "
                    "remove it from MQTT Admin if it is not wanted",
                    dev["device_name"], dev["identity"] or "no identifier", dev["entity_count"],
                )
        except Exception as _g_exc:
            logger.debug(f"Ghost device sweep skipped: {_g_exc!r}")

    asyncio.create_task(_sweep_ghost_devices(), name="ghost_device_sweep")

    # --- Actionable taps arrive over the WebSocket, not a pasted webhook -----
    # The return leg was a rest_command the user maintains, pointing at a URL
    # that has to stay reachable from Home Assistant. Every failure of it is
    # silent here: pushes were delivering 5/5 at 200 with not one response ever
    # recorded, and a tap against a wrong URL is indistinguishable from no tap.
    # Subscribing to mobile_app_notification_action removes the whole surface —
    # no YAML, no URL, no port. The webhook still works for anyone already set
    # up; both routes end in the same handler.
    async def _ha_event_listener():
        try:
            if str(await db.get_config_value("ha_event_listener_enabled", "1")) != "1":
                logger.info("HA event listener disabled by configuration")
                return
            from src.services import ha_event_listener
            await ha_event_listener.run_forever()
        except asyncio.CancelledError:
            raise
        except Exception as _e_exc:
            logger.warning(f"HA event listener stopped: {_e_exc!r}")

    asyncio.create_task(_ha_event_listener(), name="ha_event_listener")

    # --- Stage 10: Scheduler liveness monitor (v0.4.8, 2026-08-06) ---
    # Independent asyncio task on the uvicorn event loop that watches
    # pricing_eval_log freshness and rebuilds the AsyncIOScheduler if it
    # stalls silently. Root incident: 2026-08-06 02:01:32 SQLite lock
    # killed the scheduler wakeup for ~5 h without any external symptom.
    # See src/services/scheduler_liveness.py for the full postmortem.
    if is_setup_complete and automation_engine:
        from src.services.scheduler_liveness import run_monitor
        asyncio.create_task(run_monitor(), name="scheduler_liveness")
        logger.info("Scheduler liveness monitor: task spawned")

    logger.info("✅ Startup complete — SmartDispatch B1 online")
    yield

    # --- Shutdown ---
    logger.info("Shutting down gracefully...")

    # Update Security Snapshot Hash on clean shutdown
    from src.services.security_checker import update_security_snapshot
    try:
        await update_security_snapshot()
        logger.info("🔒 Saved clean security snapshot hash on shutdown")
    except Exception as e:
        logger.warning(f"Failed to save security snapshot hash on shutdown: {e}")

    await backup_manager.stop()
    bms_history_manager.stop()
    if automation_engine:
        await automation_engine.stop()
    await pricing_registry.stop_all()
    await registry.stop_all()
    if publisher:
        await publisher.stop()
    if listener:
        await listener.stop()




# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(
    title="FranklinWH HA Integrator",
    version=__version__,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url=None,
)

class _RevalidatingStatic(StaticFiles):
    """Serve static files with "revalidate before use".

    StaticFiles sends ETag and Last-Modified but no Cache-Control, which leaves
    the browser to guess a freshness lifetime. Guessing means a changed asset can
    be served from cache without ever asking us — and the tab components are
    loaded by URL with a hand-maintained `?v=` in app.js, so an asset whose
    version nobody remembered to bump stays stale indefinitely.

    That is not hypothetical: the Security tab shipped with new markup against a
    cached component that predated it, and Alpine threw ReferenceError on every
    piece of state the new template bound. The panels simply did not render.

    `no-cache` does not mean "do not store" — it means "revalidate first". The
    ETag still does its job: unchanged files answer 304 with no body, so the cost
    is one conditional request, and a changed file can no longer be missed.
    """

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response



def _compute_assets_id() -> str:
    """Content hash of the front-end assets, computed once at start-up.

    Deliberately content-based rather than mtime-based: a redeploy that rewrites
    an identical file — docker cp, a repo sync, a rebuild — must not look like a
    change, or the client prompts to reload for nothing and the prompt stops
    meaning anything.

    Not boot_id, for the same reason: the process restarts far more often than
    the assets change.
    """
    import hashlib

    digest = hashlib.sha256()
    root = Path(__file__).parent / "static"
    for path in sorted(root.rglob("*")):
        if path.suffix not in (".js", ".css") or not path.is_file():
            continue
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


_ASSETS_ID = _compute_assets_id()

_STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", _RevalidatingStatic(directory=str(_STATIC_DIR)), name="static")

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
from src.routes import admin, api_gateways, api_mqtt, api_metrics, api_control, api_phase9, api_terminal, api_system, api_scheduler, api_pricing, api_ha, api_models, api_automation, api_smart_dispatch, api_solar, api_weather, api_map, api_security, api_docs, api_persona, api_account, api_setup  # noqa: E402
from src.middleware.authz import Tier, mount  # noqa: E402
from src.middleware.auth import AdminAuthMiddleware  # noqa: E402
from src.middleware.telemetry import TelemetryMiddleware  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

# Registered before AdminAuthMiddleware so it runs OUTSIDE it: Starlette applies
# middleware in reverse registration order, and counting must observe the route
# that was actually matched, including ones auth rejected. It records nothing
# until the user opts in, and never transmits (GH #40).
app.add_middleware(TelemetryMiddleware)
app.add_middleware(AdminAuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex="https?://.*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Router mounting with authorization tiers ─────────────────────────────────
# Every router declares a Tier here. `mount()` REFUSES a missing tier, so a new
# router cannot reach production unprotected — it crashes at startup in
# development instead. 20 declarations rather than ~310 per-route decorators,
# and new routes inside an existing router inherit their tier automatically.
# See src/middleware/authz.py for why this lives at mount time rather than in
# the auth middleware or on individual routes.
#
#   ADMIN_ONLY   admin for everything
#   OPERATIONAL  viewer reads, operator acts   (battery / dispatch control)
#   TELEMETRY    viewer reads, admin mutates   (read-mostly surfaces)
#   SELF_SERVICE any authenticated principal, acting only on themselves
mount(app, admin.router, Tier.TELEMETRY)
mount(app, api_security.router, Tier.ADMIN_ONLY, prefix="/api")
mount(app, api_account.router, Tier.SELF_SERVICE, prefix="/api")
mount(app, api_gateways.router, Tier.OPERATIONAL, prefix="/api")
# The first-run wizard is reachable before anything is configured, so it sits
# at the same tier as the gateway routes it orchestrates.
mount(app, api_setup.router, Tier.OPERATIONAL, prefix="/api")
mount(app, api_mqtt.router, Tier.ADMIN_ONLY, prefix="/api")
mount(app, api_metrics.router, Tier.TELEMETRY, prefix="/api")
mount(app, api_control.router, Tier.OPERATIONAL, prefix="/api")
mount(app, api_phase9.router, Tier.OPERATIONAL, prefix="/api")
mount(app, api_terminal.router, Tier.ADMIN_ONLY, prefix="/api")
mount(app, api_system.router, Tier.ADMIN_ONLY, prefix="/api")
mount(app, api_scheduler.router, Tier.OPERATIONAL, prefix="/api")
mount(app, api_pricing.router, Tier.OPERATIONAL, prefix="/api")
mount(app, api_ha.router, Tier.OPERATIONAL, prefix="/api")
mount(app, api_models.router, Tier.ADMIN_ONLY, prefix="/api")
mount(app, api_automation.router, Tier.OPERATIONAL, prefix="/api")
mount(app, api_smart_dispatch.router, Tier.OPERATIONAL, prefix="/api/smart_dispatch")
mount(app, api_persona.router, Tier.ADMIN_ONLY, prefix="/api")
mount(app, api_solar.router, Tier.OPERATIONAL)      # prefix: /api/solar
mount(app, api_weather.router, Tier.TELEMETRY)      # prefix: /api/weather
mount(app, api_map.router, Tier.OPERATIONAL)         # prefix: /api/map
mount(app, api_docs.router, Tier.TELEMETRY)         # /docs and /docs/{name}


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------
@app.get("/api/health", tags=["system"])
async def health():
    state = get_app_state()
    registry = state.get("registry")
    publisher = state.get("publisher")
    # Scheduler liveness — populated by src.services.scheduler_liveness.run_monitor()
    try:
        from src.services.scheduler_liveness import get_state as _get_liveness
        liveness = _get_liveness()
    except Exception:
        liveness = None
    return {
        "status": "ok",
        "version": state.get("version", __version__),
        "env": state.get("env", "unknown"),
        "boot_id": state.get("boot_id", "unknown"),
        # Changes only when a JS/CSS file's contents change. The dashboard
        # compares it across polls and offers a reload — it never reloads on its
        # own, because a reload discards whatever the user was part-way through
        # typing.
        "assets_id": _ASSETS_ID,
        "gateways": registry.count() if registry else 0,
        "gateways_running": registry.running_count() if registry else 0,
        "mqtt_connected": publisher.connected if publisher else False,
        "scheduler_liveness": liveness,
    }


# ---------------------------------------------------------------------------
# Standalone Baremetal TLS Execution
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    from src.config.manager import AppConfig
    import os

    # Load configuration to look for SSL keys
    os.environ["ENV"] = "dev"  # Force config to load cleanly
    cfg = AppConfig.load()
    
    uvicorn_kwargs = {
        "host": "0.0.0.0",
        "port": int(os.environ.get("PORT", 8099)),
    }
    
    # Inject TLS termination if keys/paths are provided (BKL-SEC-01)
    tls_enabled = cfg.tls_enabled or (cfg.ssl_keyfile and cfg.ssl_certfile)
    cert_path = cfg.tls_cert_path if cfg.tls_enabled else cfg.ssl_certfile
    key_path = cfg.tls_key_path if cfg.tls_enabled else cfg.ssl_keyfile

    if tls_enabled and cert_path and key_path:
        uvicorn_kwargs["ssl_keyfile"] = key_path
        uvicorn_kwargs["ssl_certfile"] = cert_path
        logger.info(f"🔒 Initiating standalone Uvicorn with TLS termination from {cert_path}")
        
        # Enforce mTLS client verification if active
        if cfg.mtls_enabled and cfg.client_ca_path:
            import ssl
            uvicorn_kwargs["ssl_ca_certs"] = cfg.client_ca_path
            uvicorn_kwargs["ssl_cert_reqs"] = ssl.CERT_REQUIRED
            logger.info(f"🔒 Enforcing Mutual TLS (mTLS) client certificate verification using CA {cfg.client_ca_path}")

    uvicorn.run("src.main:app", **uvicorn_kwargs)
