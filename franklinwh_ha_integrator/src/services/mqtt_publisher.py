"""
MQTTPublisher — bridges GatewayService poll data to Home Assistant via MQTT Discovery.

Design:
  - Queue-based: callers enqueue (topic, payload, retain) tuples
  - A background task drains the queue through the MQTT connection
  - Reconnects automatically on disconnect (exponential backoff)
  - Publishes HA Discovery payloads once per gateway profile load
  - Publishes state updates on every poll cycle
  - Publishes availability (online/offline) per gateway
"""
import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


#: Warn once per process, not once per entity — there are ~100 per poll.
_WARNED_FULL_SERIAL = False


def normalise_state_value(ent, value):
    """The published form of `value` for `ent`, or None to skip publishing.

    Shared by the poll-driven state publish and the post-command echo, so a
    command confirmation cannot disagree with the next poll about how the same
    value is spelled.
    """
    # Phase 116: HA binary_sensor AND switch require ON/OFF
    # Python bool str() produces "True"/"False" which HA cannot map to ON/OFF
    if ent.ha_type in ("binary_sensor", "switch"):
        return "ON" if str(value).lower() in ("true", "1", "on") else "OFF"

    # DEF-SELECT-NORM: Normalise against the entity's declared options.
    # franklinwh-cloud returns 'Time-Of-Use' (capital O); our options have
    # 'Time-of-Use' (lowercase o). HA rejects non-matching values on a select,
    # and on an enum sensor.
    #
    # This applied to selects only, so the Operating Mode *sensor* published the
    # raw cloud spelling while the Operating Mode *select* published the
    # canonical one — the same fact, at the same instant, spelled two ways:
    #   sensor.fhp_operating_mode_2  'Time-Of-Use'
    #   select.fhp_operating_mode    'Time-of-Use'
    # Any template or automation comparing them silently never matched.
    if ent.options and value is not None:
        _val_lower = str(value).lower()
        _match = next((o for o in ent.options if o.lower() == _val_lower), None)
        if _match:
            return _match

    # DEF-ENUM-EMPTY: HA device_class='enum' requires one of the defined
    # options. When not in TOU mode, tariff sensors legitimately have no value.
    if ent.device_class == "enum" and str(value) == "":
        return None

    return value


def battery_short_id(bat: dict) -> str:
    """Resolve a battery's short id, or "" when it has no usable serial.

    Callers MUST skip an empty result. The discovery publisher did not, while
    the state publisher and the unpublish route both did — so a battery
    reporting a blank serial had its discovery config published as
    `franklinwh__soc` and friends, creating entities in Home Assistant that
    then never received state (state path skipped them) and could never be
    tombstoned (unpublish skipped them too). Permanently unavailable, and
    unremovable by the app that created them. Six such topics were found
    retained on a live broker.

    bat.get("full_serial", bat.get("short_id", "")) is not sufficient: the
    default applies only when the key is absent, so a present-but-null serial
    fell straight through to len(None) and raised TypeError.
    """
    serial = bat.get("full_serial") or bat.get("short_id") or ""
    serial = str(serial).strip()
    return serial[-8:] if len(serial) >= 8 else serial

RECONNECT_BASE = 5    # seconds
RECONNECT_MAX  = 60   # seconds


@dataclass
class PublishMessage:
    topic: str
    payload: str
    retain: bool = False
    qos: int = 0


class MQTTPublisher:
    """
    Manages the MQTT connection and publishes discovery + state messages.

    :param host:             MQTT broker hostname
    :param port:             MQTT broker port
    :param username:         Optional broker username
    :param password:         Optional broker password
    :param topic_prefix:          State topic prefix (default "franklinwh")
    :param discovery_prefix:      HA discovery prefix (default "homeassistant")
    :param entity_prefix_template: Template for entity unique_id prefix.
                                   Supports ``{short_id}`` token.
                                   Default ``"franklinwh_{short_id}_"`` matches the
                                   current deployed format — stored in app_config DB
                                   as ``mqtt_entity_prefix``.
    """

    def __init__(
        self,
        host: str,
        port: int = 1883,
        username: str = "",
        password: str = "",
        client_id: str = "franklinwh_bridge",
        qos: int = 0,
        retain_discovery: bool = True,
        topic_prefix: str = "franklinwh",
        discovery_prefix: str = "homeassistant",
        entity_prefix_template: str = "franklinwh_{short_id}_",
        device_name_template: str = "FranklinWH {model} {serial4}",
        slug_aliases: dict | None = None,
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.client_id = client_id
        self.default_qos = qos
        self.retain_discovery = retain_discovery
        self.topic_prefix = topic_prefix
        self.discovery_prefix = discovery_prefix
        # Entity unique_id prefix template — stored in DB as mqtt_entity_prefix.
        # {short_id} is resolved per-gateway at publish time.
        self.entity_prefix_template = entity_prefix_template
        self.device_name_template = device_name_template
        # Slug overrides, applied before the prefix. The prefix has been
        # templated and migratable for a long time while the slug stayed
        # hardcoded in models/entities.py — so an install whose dashboards were
        # built against another FranklinWH integration could match the prefix
        # and still miss on a renamed measurement, with no way to fix it here.
        self.slug_aliases = dict(slug_aliases or {})

        self._queue: asyncio.Queue[PublishMessage] = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self.connected = False

        # ── Runtime stats ─────────────────────────────────────────
        self.messages_published: int = 0
        self.reconnect_count: int = 0
        self.last_connected_at: Optional[float] = None
        self.last_error: Optional[str] = None
        # Log a credential rejection once, not once a minute forever.
        self._auth_failure_logged: bool = False
        self.started_at: Optional[float] = None
        self.last_publish_time: Optional[float] = None

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the publish loop as a background task."""
        if self._task and not self._task.done():
            return
        self.started_at = time.time()
        self._task = asyncio.create_task(self._run_loop(), name="mqtt-publisher")
        logger.info(f"MQTTPublisher started → {self.host}:{self.port}")

    async def stop(self) -> None:
        """Cancel the publish loop."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.connected = False
        logger.info("MQTTPublisher stopped")

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ------------------------------------------------------------------
    # Public publish API
    # ------------------------------------------------------------------

    def enqueue(self, topic: str, payload: str, retain: bool = False, qos: int = 0) -> None:
        """Non-blocking: push a message onto the publish queue."""
        self._queue.put_nowait(PublishMessage(topic, payload, retain, qos))
        self.last_publish_time = time.time()

    def _make_uid(self, short_id: str, slug: str, *,
                  full_serial: str = "",
                  gateway_name: str = "",
                  site_id: str = "",
                  model: str = "") -> str:
        """Resolve the entity unique_id from the configured prefix template.

        Supported tokens:
            ``{short_id}``      → last 8 chars of serial, e.g. ``99900001``
            ``{full_serial}``   → full serial, lowercased + sanitised
            ``{gateway_name}``  → user-defined gateway label, lowercased + sanitised
            ``{site_id}``       → cloud site ID, lowercased + sanitised
            ``{model}``         → model, lowercased + sanitised, e.g. ``agate_x_01_au``
            ``{serial4}``       → last 4 of the serial, e.g. ``0091``

        Sanitisation: whitespace/hyphens/dots → ``_``, collapse ``__``, strip leading/trailing ``_``.
        """
        def _sanitise(s: str) -> str:
            import re
            s = s.lower().strip()
            s = re.sub(r"[\s\-\.]+", "_", s)   # spaces/hyphens/dots → _
            s = re.sub(r"_+", "_", s)            # collapse runs
            return s.strip("_")

        # The alias is part of the identity, so it must be applied before the
        # prefix is rendered — the unique_id is what Home Assistant keys the
        # entity on, and changing it is a migration, exactly like a prefix
        # change.
        slug = self.slug_aliases.get(slug, slug)

        prefix = self.entity_prefix_template
        prefix = prefix.replace("{short_id}",     short_id)
        # {full_serial} is the one place a serial gets lower-cased, because
        # Home Assistant requires entity ids to be lowercase. A lower-cased
        # serial is genuinely hard to read — the alpha run in the middle
        # ("A02F" → "a02f") stops standing out against the digits, and
        # transcribing between the two forms has gone wrong repeatedly. Say so
        # once per process rather than silently producing one.
        if "{full_serial}" in prefix and full_serial:
            global _WARNED_FULL_SERIAL
            if not _WARNED_FULL_SERIAL:
                _WARNED_FULL_SERIAL = True
                logger.warning(
                    "entity prefix uses {full_serial}, which Home Assistant "
                    "forces to lowercase (%s). Serials are written uppercase "
                    "everywhere else; prefer {short_id}, or {model} with "
                    "{serial4}.", _sanitise(full_serial),
                )
        prefix = prefix.replace("{full_serial}",  _sanitise(full_serial)  if full_serial  else short_id)
        prefix = prefix.replace("{gateway_name}", _sanitise(gateway_name) if gateway_name else short_id)
        prefix = prefix.replace("{site_id}",      _sanitise(site_id)      if site_id      else short_id)
        # {model} and {serial4} exist so a template can reproduce the scheme a
        # multi-gateway site already uses —
        # "franklinwh_{model}_{serial4}_" → "franklinwh_agate_x_01_au_0091_".
        # The SKU is stripped first: the model arrives as
        # "aGate X-01-AU (AGT-R1V1-AU)" and the part number does not belong in
        # an entity id.
        _model = (model or "").strip()
        if "(" in _model and _model.endswith(")"):
            _model = _model[:_model.index("(")].strip()
        prefix = prefix.replace("{model}",   _sanitise(_model) if _model else "agate")
        prefix = prefix.replace("{serial4}", short_id[-4:] if short_id else "")
        return f"{prefix}{slug}"

    def publish_availability(self, full_serial: str, online: bool) -> None:
        """Publish availability for a gateway (online / offline)."""
        short_id = full_serial[-8:]
        topic = f"{self.topic_prefix}/{short_id}/availability"
        self.enqueue(topic, "online" if online else "offline", retain=True)

    def publish_device_info(
        self, full_serial: str, gateway_name: str, model: str,
        firmware: str, app_version: str
    ) -> None:
        """Publish retained device metadata topics matching FEM's device/* structure."""
        import datetime
        import json
        out_uid = getattr(self, "instance_uid", "FHAI")
        short_id = full_serial[-8:]
        base = f"{self.topic_prefix}/{short_id}/device"
        ts = datetime.datetime.utcnow().isoformat() + "Z"
        
        self.enqueue(f"{base}/serial_number", json.dumps({"value": full_serial, "ts": ts}), retain=True)
        self.enqueue(f"{base}/model", json.dumps({"value": model, "ts": ts}), retain=True)
        if firmware:
            self.enqueue(f"{base}/firmware_version", json.dumps({"value": firmware, "ts": ts}), retain=True)
        self.enqueue(f"{base}/cloud_software_version", json.dumps({"value": f"{out_uid}: v{app_version}", "ts": ts}), retain=True)
        self.enqueue(f"{base}/name", json.dumps({"value": gateway_name, "ts": ts}), retain=True)

    def publish_discovery(self, full_serial: str, gateway_name: str, profile: dict,
                          batteries: list[dict], stats: dict = None,
                          tou_preset_options: list[str] = None) -> None:
        """
        Publish HA MQTT Discovery config payloads for all entities on a gateway.
        Topic pattern: homeassistant/{ha_type}/franklinwh_{short_id}_{slug}/config
        State topics:  franklinwh/{short_id}/{state_group}/{slug}
        """
        from src.models.entities import get_entities_for_profile, BATTERY_ACCESSORY_ENTITIES
        short_id = full_serial[-8:]

        # 1. Primary aGate Device (Flattened Topology - FEM Parity)
        agate_device_payload = self._build_device_payload(full_serial, gateway_name, profile)
        availability_topic = f"{self.topic_prefix}/{short_id}/availability"

        # Overrides ride along on the profile: this method is synchronous and
        # they live in the database. Set in gateway_service beside the firmware
        # and hardware summary.
        entities = get_entities_for_profile(profile, profile.get("_gate_overrides") or {})
        for ent in entities:
            disc_topic = (
                f"{self.discovery_prefix}/{ent.ha_type}/"
                f"{self._make_uid(short_id, ent.slug)}/config"
            )
            state_topic = f"{self.topic_prefix}/{short_id}/{ent.state_group}/{ent.slug}"

            # Circuit entities: use the user-defined name from the cloud API (e.g. "Circuit Test").
            # The slug is IMMUTABLE (AP-2). Only the HA display label changes dynamically.
            # Falls back to the EntityDef default name ("Circuit N Power", "Circuit N") if no
            # custom name is in stats (e.g. on the very first poll before circuit info is fetched).
            custom_ent_name = ent.name
            if stats and ent.slug.startswith("smart_circuit_"):
                # Determine circuit index: smart_circuit_1, smart_circuit_2_kw, smart_circuit_3_power_kw etc.
                import re as _re
                m = _re.search(r"smart_circuit_(\d+)", ent.slug)
                if m:
                    n = m.group(1)
                    cloud_name = stats.get(f"smart_circuit_{n}_name")  # e.g. "Circuit Test"
                    if cloud_name:
                        # Power sensors: append " Power"
                        if ent.slug.endswith("_kw") or ent.slug.endswith("_power_kw"):
                            custom_ent_name = f"{cloud_name} Power"
                        # Energy sensors: append " Daily Energy"
                        elif ent.slug.endswith("_energy_kwh"):
                            custom_ent_name = f"{cloud_name} Daily Energy"
                        # Switch entities: use cloud name directly ("Circuit 1", "Circuit Test", etc.)
                        else:
                            custom_ent_name = cloud_name

            # Flat Hierarchy: All entities anchor explicitly to the primary aGate device
            active_device = agate_device_payload
            
            # Override options dynamically for tou_saved_dispatches
            dynamic_options = None
            if ent.slug == "tou_saved_dispatches" and tou_preset_options:
                dynamic_options = tou_preset_options

            payload = self._build_entity_payload(
                ent, short_id, gateway_name, active_device,
                state_topic, availability_topic, custom_name=custom_ent_name,
                options_override=dynamic_options
            )
            self.enqueue(disc_topic, json.dumps(payload), retain=self.retain_discovery, qos=self.default_qos)

        # Battery accessories — use bat_short (last 8 of bat serial) for topic/slug
        for bat_idx, bat in enumerate(batteries):
            bat_short = battery_short_id(bat)
            if not bat_short:
                logger.warning(
                    f"[{short_id}] battery #{bat_idx} has no usable serial — "
                    f"skipping discovery. Publishing it would create entities "
                    f"that never receive state and cannot be unpublished."
                )
                continue
            for ent in BATTERY_ACCESSORY_ENTITIES:
                disc_topic = (
                    f"{self.discovery_prefix}/{ent.ha_type}/"
                    f"{self._make_uid(bat_short, ent.slug)}/config"
                )
                state_topic = (
                    f"{self.topic_prefix}/{short_id}/accessories/"
                    f"{bat_short}/{ent.slug}"
                )
                
                # Battery accessory naming: no "aPower" prefix (not user-requested).
                # Single battery: use entity name as-is ("State of Charge").
                # Multiple batteries: prefix by index ("Battery 1 State of Charge").
                if len(batteries) > 1:
                    custom_name = f"Battery {bat_idx + 1} {ent.name}"
                else:
                    custom_name = ent.name
                
                payload = self._build_entity_payload(
                    ent, bat_short,
                    "",
                    agate_device_payload, state_topic, availability_topic, custom_name=custom_name
                )
                self.enqueue(disc_topic, json.dumps(payload), retain=self.retain_discovery, qos=self.default_qos)

        logger.info(f"[{short_id}] Discovery published ({len(entities)} entities, {len(batteries)} batteries)")

        # Tombstone any circuit-3 entities that are suppressed on this hardware.
        # Publishes an empty retained payload to clear stale HA discovery entries.
        # This handles the case where circuit_3 was previously published (before capability
        # gating was added) and is now retained on the broker.
        published_slugs = {e.slug for e in entities}
        _tombstone_slugs = [
            "smart_circuit_3",
            "smart_circuit_3_power_kw",
            "smart_circuit_3_energy_kwh",
            # "Last Update" — a timestamp of the last poll. Removed as noise:
            # every other entity already carries its own freshness, and this
            # one told the user nothing they could act on. Listed here so the
            # retained config is cleared; deleting the EntityDef alone would
            # leave the entity behind, rebuilt from the broker on every restart.
            "last_update_time",
        ]
        _tombstone_types = {
            "smart_circuit_3": "switch",
            "smart_circuit_3_power_kw": "sensor",
            "smart_circuit_3_energy_kwh": "sensor",
            "last_update_time": "sensor",
        }
        for slug in _tombstone_slugs:
            if slug not in published_slugs:
                ha_type = _tombstone_types[slug]
                tomb_topic = (
                    f"{self.discovery_prefix}/{ha_type}/"
                    f"{self._make_uid(short_id, slug)}/config"
                )
                self.enqueue(tomb_topic, "", retain=True, qos=self.default_qos)
                logger.info(f"[{short_id}] Tombstoned suppressed entity: {slug}")

        # DEF-GRID-STATE-MIGRATION: grid_connection_state was changed from ha_type='select'
        # to ha_type='sensor' (select requires command_topic which we don't provide for
        # this read-only diagnostic). Always tombstone the old select discovery topic so HA
        # removes the stale broken select entity from its registry.
        _old_grid_select_topic = (
            f"{self.discovery_prefix}/select/"
            f"{self._make_uid(short_id, 'grid_connection_state')}/config"
        )
        self.enqueue(_old_grid_select_topic, "", retain=True, qos=self.default_qos)
        logger.debug(f"[{short_id}] Tombstoned migrated entity: select/grid_connection_state → sensor")

    # Hub sentinel is eliminated due to flat topology requirements.

    def publish_entity_state(self, full_serial: str, slug: str, value) -> bool:
        """Publish one entity's state now. Returns True if it was published.

        None of the control entities are declared optimistic, so Home Assistant
        holds the previous value until the state topic confirms the new one.
        Confirmation only ever arrived on the next poll — and polls are dropped
        whenever the cloud returns stale data, which on a live gateway happened
        23 times in one session. A dropdown therefore snapped back to its old
        value after a successful change and could stay there for minutes.

        This is the confirmation, sent only after the command succeeded. If the
        hardware disagrees, the next poll overwrites it — the echo is a
        confirmation, not a claim of truth.
        """
        from src.models.entities import AGATE_ENTITIES

        ent = next((e for e in AGATE_ENTITIES if e.slug == slug), None)
        if ent is None or not ent.state_group:
            return False

        published = normalise_state_value(ent, value)
        if published is None:
            return False

        short_id = full_serial[-8:]
        topic = f"{self.topic_prefix}/{short_id}/{ent.state_group}/{ent.slug}"
        self.enqueue(topic, str(published), qos=self.default_qos)
        return True

    def publish_state(self, full_serial: str, stats: dict) -> None:
        """
        Publish state values from a normalised stats dict.
        Topic: franklinwh/{short_id}/{state_group}/{slug}
        Called on every poll cycle by GatewayService.on_data.
        """
        from src.models.entities import AGATE_ENTITIES, BATTERY_ACCESSORY_ENTITIES, extract_stat
        short_id = full_serial[-8:]
        
        # 1. Agate Parent States
        for ent in AGATE_ENTITIES:
            if not ent.stat_path:
                continue
            value = extract_stat(stats, ent.stat_path)
            # Phase D5: Only skip read-only sensors on None.
            # Control entities always have a default in the normalised dict — never skip them.
            if value is None and not ent.is_control:
                continue
            if value is None:
                continue  # control stat genuinely missing even after normaliser — skip safely
            value = normalise_state_value(ent, value)
            if value is None:
                continue
            topic = f"{self.topic_prefix}/{short_id}/{ent.state_group}/{ent.slug}"
            self.enqueue(topic, str(value), qos=self.default_qos)
            
        # 2. Battery Accessory States — use bat_short for topic
        batteries = stats.get("bms_units", [])
        for bat in batteries:
            bat_short = battery_short_id(bat)
            if not bat_short:
                continue
            for ent in BATTERY_ACCESSORY_ENTITIES:
                if not ent.stat_path: continue
                # Unpack localized structural path
                stripped_path = ent.stat_path.replace("bms.", "").replace("battery.", "")
                value = extract_stat(bat, stripped_path)
                if value is None:
                    continue
                topic = f"{self.topic_prefix}/{short_id}/accessories/{bat_short}/{ent.slug}"
                self.enqueue(topic, str(value), qos=self.default_qos)

    # ------------------------------------------------------------------
    # Internal — MQTT connection loop
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        """Reconnecting publish loop."""
        import aiomqtt
        backoff = RECONNECT_BASE
        while True:
            try:
                client_kwargs = dict(
                    hostname=self.host, 
                    port=self.port,
                    identifier=self.client_id
                )
                if self.username:
                    client_kwargs["username"] = self.username
                if self.password:
                    client_kwargs["password"] = self.password

                async with aiomqtt.Client(**client_kwargs) as client:
                    self.connected = True
                    self.last_connected_at = asyncio.get_event_loop().time()
                    backoff = RECONNECT_BASE  # reset on successful connect
                    if self.reconnect_count > 0:
                        logger.info(f"MQTT reconnected to {self.host}:{self.port} (attempt {self.reconnect_count})")
                    else:
                        logger.info(f"MQTT connected to {self.host}:{self.port}")

                    while True:
                        try:
                            msg = await asyncio.wait_for(
                                self._queue.get(), timeout=25.0
                            )
                        except asyncio.TimeoutError:
                            # Keepalive heartbeat — no messages in 25s, write to
                            # socket so a dropped broker connection surfaces as MqttError
                            try:
                                await client.publish(
                                    f"franklinwh/bridge/heartbeat",
                                    payload=b"1",
                                    retain=False,
                                    qos=0,
                                )
                            except Exception:
                                raise  # triggers outer reconnect loop
                            continue

                        try:
                            await client.publish(
                                msg.topic,
                                payload=msg.payload.encode(),
                                retain=msg.retain,
                                qos=msg.qos,
                            )
                            self.messages_published += 1
                        except aiomqtt.MqttError as pub_err:
                            logger.warning(f"Publish error: {pub_err} — re-queuing")
                            self._queue.put_nowait(msg)
                            raise  # trigger reconnect
                        finally:
                            self._queue.task_done()

            except asyncio.CancelledError:
                self.connected = False
                raise

            except Exception as exc:
                self.connected = False
                self.reconnect_count += 1

                # A credential rejection cannot be retried into success —
                # nothing changes between attempts. Say what is wrong once, at
                # ERROR, then keep the retry loop quiet so the real cause is not
                # buried under a message a minute.
                from src.services import mqtt_errors

                if mqtt_errors.is_auth_failure(exc):
                    self.last_error = mqtt_errors.explain(
                        exc, host=f"{self.host}:{self.port}", username=self.username or ""
                    )
                    if not self._auth_failure_logged:
                        logger.error(f"MQTT: {self.last_error}")
                        self._auth_failure_logged = True
                    else:
                        logger.debug(f"MQTT still refusing credentials: {exc}")
                else:
                    # A refused connection can recover — the broker may start
                    # later — so keep retrying, but explain it once rather than
                    # repeating a bare errno every minute.
                    explained = mqtt_errors.explain(exc, host=f"{self.host}:{self.port}")
                    first_time = explained != self.last_error
                    self.last_error = explained
                    self._auth_failure_logged = False
                    if first_time:
                        logger.warning(f"MQTT: {explained} Retrying every {backoff}s.")
                    else:
                        logger.debug(f"MQTT still disconnected: {exc}")

                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX)

    # ------------------------------------------------------------------
    # HA Discovery payload builders
    # ------------------------------------------------------------------

    def _build_device_payload(self, full_serial: str, name: str, profile: dict) -> dict:
        import pathlib
        version = "unknown"
        try:
            version = pathlib.Path("/app/VERSION").read_text().strip()
        except Exception:
            pass
            
        firmware = profile.get("firmware", "")
        sku = profile.get("sku", "")
        # Prefer the specific model over the family. The cloud's `model_name` is
        # often just "aGate", while `model` carries "aGate X-01-AU" — and since
        # 0.6.23 the catalog resolves the hardware version into that field too.
        # Publishing the family made this device read "FranklinWH aGate 0091"
        # beside a sibling called "FranklinWH aGate X-01-AU 0091", which looks
        # like different hardware and is not.
        model_name = (
            (profile.get("model") or "").strip()
            or (profile.get("model_name") or "").strip()
            or "aGate"
        )

        # Strip the SKU, exactly as FEM does — its mqtt_publisher splits
        # "aGate X-01-AU (AGT-R1V1-AU)" into model and sku and names the device
        # from the model alone. gateway_service builds device_model_full in that
        # combined form, so without this the device reads
        # "FranklinWH aGate X-01-AU (AGT-R1V1-AU) 0091" and stops matching the
        # integration it is meant to sit beside.
        if "(" in model_name and model_name.endswith(")"):
            bracketed = model_name[model_name.index("(") + 1:-1].strip()
            model_name = model_name[:model_name.index("(")].strip() or model_name
            sku = sku or bracketed
        short_serial = full_serial[-8:]

        # "FranklinWH aGate X-01-AU 0091" — brand, model, serial suffix, and
        # nothing else. This is what FEM and the earlier integrations produced,
        # so a device list holding several of them reads consistently.
        #
        # An earlier version appended the user's gateway name in brackets —
        # "FHP (FranklinWH aGate 0091)" — which is neither this format nor
        # theirs, and looks wrong beside the siblings it is meant to match. The
        # name the user chose belongs on the entities, not bolted onto the
        # device identity.
        # Rendered from a template the user owns, not a format chosen here.
        # This name has changed three times in one release cycle because it was
        # a decision in the code rather than a setting, and every change churns
        # the device in Home Assistant. Default matches what FEM and the earlier
        # integrations produced.
        hw_name = (
            (self.device_name_template or "FranklinWH {model} {serial4}")
            .replace("{model}", model_name)
            .replace("{serial4}", short_serial[-4:])
            .replace("{short_id}", short_serial)
            .replace("{serial}", full_serial)
            .replace("{name}", (name or "").strip())
        )
        hw_name = " ".join(hw_name.split()).strip() or f"FranklinWH {short_serial[-4:]}"

        payload = {
            "identifiers": [f"franklinwh_{full_serial}"],
            "name": hw_name,
            "model": model_name,
            "manufacturer": "FranklinWH Technologies Co., Ltd",
            "serial_number": full_serial,   # surfaced as labelled field in HA 2023.6+
            "configuration_url": "http://localhost:8099",
        }

        out_uid = getattr(self, "instance_uid", "FHAI")
        if firmware:
            payload["sw_version"] = f"{firmware} ({out_uid}: v{version})"
        else:
            payload["sw_version"] = f"{out_uid}: v{version}"

        if sku:
            payload["hw_version"] = sku

        # The fleet line wins when it was resolved: "AGT-R1V1-AU · 1× APR-..."
        # tells you what the site is made of, where a bare aGate SKU does not.
        hw_summary = (profile.get("hw_summary") or "").strip()
        if hw_summary:
            payload["hw_version"] = hw_summary

        return payload

    def _build_battery_device_payload(self, bat_serial: str, via_device_id: str, bat: dict) -> dict:
        # Obsolete: We map accessories straight to aGate payload now. Kept purely if needed elsewhere
        pass

    def _build_entity_payload(
        self, ent: 'EntityDef', serial: str, device_name: str, device_payload: dict,
        state_topic: str, availability_topic: str, custom_name: str = None,
        options_override: list = None
    ) -> dict:
        uid = self._make_uid(serial, ent.slug, model=(device_payload or {}).get("model", ""))
        payload = {
            "unique_id": uid,
            # object_id is what Home Assistant builds the entity_id from.
            # Without it HA falls back to the *device name* plus the entity
            # name, so a gateway labelled "FHP" produced
            # sensor.fhp_state_of_charge — a name that says nothing about which
            # gateway it belongs to, and that changes if the device is renamed.
            # A multi-gateway site needs the gateway in the id, which is
            # precisely what the entity prefix template already expresses.
            "object_id": uid,
            "name": custom_name or ent.name,
            "state_topic": state_topic,
            "availability_topic": availability_topic,
            "device": device_payload,
        }
        if ent.unit:
            payload["unit_of_measurement"] = ent.unit
        if ent.device_class:
            payload["device_class"] = ent.device_class
        if ent.state_class:
            payload["state_class"] = ent.state_class
        if ent.icon:
            payload["icon"] = ent.icon
        if ent.entity_category:
            payload["entity_category"] = ent.entity_category
        if ent.is_control and ent.command_topic_template:
            # Change: Emit command_topic against full_serial to resolve mismatched Command Listener drops
            payload["command_topic"] = ent.command_topic_template.format(short_id=serial)
        # Use caller-supplied override if provided, otherwise fall back to EntityDef default
        effective_options = options_override if options_override is not None else ent.options
        if effective_options:
            payload["options"] = effective_options
        if ent.min_val is not None:
            payload["min"] = ent.min_val
            payload["max"] = ent.max_val
            payload["step"] = ent.step
        # Auto-inject device_class='enum' for sensors with options but no explicit device_class.
        # HA 2024.x+ requires device_class='enum' when options= is present on a sensor entity.
        if ent.ha_type == "sensor" and effective_options and not ent.device_class:
            payload["device_class"] = "enum"
        # HA switch requires explicit ON/OFF payload mappings — without these the state
        # topic value must exactly match the default strings which Python bool str() breaks.
        if ent.ha_type == "switch":
            payload["payload_on"] = "ON"
            payload["payload_off"] = "OFF"
            payload["state_on"] = "ON"
            payload["state_off"] = "OFF"
        return payload

    def tombstone_discovery(self, ha_type: str, unique_id: str) -> None:
        """
        Tombstone (remove) a single HA entity by publishing a null retained payload
        to its discovery topic. HA auto-removes the entity when it receives a null/empty
        retained discovery message.

        This is FHAI's MQTT-native repair approach — preferable to HA WebSocket registry
        deletes because the MQTT broker controls entity lifecycle for FHAI-published entities.

        Args:
            ha_type:   HA component type (e.g. 'sensor', 'switch', 'number')
            unique_id: Full unique_id of the entity (e.g. 'franklinwh_99900001_grid_status')
                       Used to reconstruct the discovery topic.
        """
        disc_topic = f"{self.discovery_prefix}/{ha_type}/{unique_id}/config"
        # Null payload on a retained topic removes it from HA's discovery registry
        self.enqueue(disc_topic, "", retain=True, qos=0)
        logger.info(f"[Repair] Tombstoned discovery: {disc_topic}")

    def tombstone_all_discovery(
        self,
        short_id: str,
        batteries: list[dict],
        profile: dict,
        override_prefix_template: str | None = None,
    ) -> int:
        """
        Tombstone ALL discovery topics for a gateway using a given prefix template.
        Used by the migration tool to clear old-prefix entities before republishing
        under a new prefix.

        Args:
            short_id:                 Gateway short serial (8 chars)
            batteries:                List of battery dicts (from last_data bms_units)
            profile:                  Gateway profile dict (for entity list)
            override_prefix_template: If set, use this prefix instead of self.entity_prefix_template.
                                      Pass the OLD prefix here when migrating.
        Returns:
            Number of discovery topics tombstoned.
        """
        from src.models.entities import get_entities_for_profile, BATTERY_ACCESSORY_ENTITIES
        old_template = override_prefix_template or self.entity_prefix_template
        cleared = 0

        # aGate entities
        for ent in get_entities_for_profile(profile):
            prefix = old_template.replace("{short_id}", short_id)
            uid = f"{prefix}{ent.slug}"
            self.tombstone_discovery(ent.ha_type, uid)
            cleared += 1

        # Battery accessory entities
        for bat in batteries:
            bat_short = battery_short_id(bat)
            if not bat_short:
                continue
            for ent in BATTERY_ACCESSORY_ENTITIES:
                prefix = old_template.replace("{short_id}", bat_short)
                uid = f"{prefix}{ent.slug}"
                self.tombstone_discovery(ent.ha_type, uid)
                cleared += 1

        logger.info(f"[Migration] Tombstoned {cleared} discovery topics for {short_id} (prefix: {old_template!r})")
        return cleared

    async def publish_forecast_loads_discovery(self) -> None:
        """
        Publish HA MQTT Discovery config payloads for all configured Forecast Loads.
        For each Forecast Load, we publish an independent HA Device containing:
          - Power Sensor (kW)
          - Energy Sensor (kWh)
          - Switch (Enabled Status - On/Off)
          - Binary Sensor (Active status - On/Off)
        """
        from src.services import db
        try:
            loads = await db.get_all_forecast_loads()
        except Exception as e:
            logger.error(f"Failed to fetch forecast loads for discovery: {e}")
            return

        for load in loads:
            load_id = load["id"]
            load_name = load["name"]
            
            # Build HA Device payload
            device_payload = {
                "identifiers": [f"hems_load_{load_id}"],
                "name": f"HEMS Load: {load_name}",
                "model": "HEMS Forecast Load",
                "manufacturer": "HEMS Smart Dispatch",
                "via_device": "franklinwh_bridge",  # anchor to bridge
            }
            
            availability_topic = f"{self.topic_prefix}/loads/{load_id}/availability"
            
            # Publish availability = online
            self.enqueue(availability_topic, "online", retain=True)
            
            # 1. Power Sensor (kW)
            power_disc_topic = f"{self.discovery_prefix}/sensor/hems_load_{load_id}_power/config"
            power_payload = {
                "unique_id": f"hems_load_{load_id}_power_kw",
                "name": "Power",
                "state_topic": f"{self.topic_prefix}/loads/{load_id}/power_kw",
                "unit_of_measurement": "kW",
                "device_class": "power",
                "state_class": "measurement",
                "icon": "mdi:bolt",
                "device": device_payload,
                "availability_topic": availability_topic,
            }
            self.enqueue(power_disc_topic, json.dumps(power_payload), retain=self.retain_discovery)
            
            # 2. Energy Sensor (kWh)
            energy_disc_topic = f"{self.discovery_prefix}/sensor/hems_load_{load_id}_energy/config"
            energy_payload = {
                "unique_id": f"hems_load_{load_id}_energy_kwh",
                "name": "Energy",
                "state_topic": f"{self.topic_prefix}/loads/{load_id}/energy_kwh",
                "unit_of_measurement": "kWh",
                "device_class": "energy",
                "state_class": "total_increasing",
                "icon": "mdi:chart-simple",
                "device": device_payload,
                "availability_topic": availability_topic,
            }
            self.enqueue(energy_disc_topic, json.dumps(energy_payload), retain=self.retain_discovery)
            
            # 3. Switch (Enabled Status)
            switch_disc_topic = f"{self.discovery_prefix}/switch/hems_load_{load_id}_enabled/config"
            switch_payload = {
                "unique_id": f"hems_load_{load_id}_enabled",
                "name": "Enabled",
                "state_topic": f"{self.topic_prefix}/loads/{load_id}/enabled",
                "command_topic": f"{self.topic_prefix}/loads/{load_id}/enabled/set",
                "payload_on": "ON",
                "payload_off": "OFF",
                "state_on": "ON",
                "state_off": "OFF",
                "icon": "mdi:power",
                "device": device_payload,
                "availability_topic": availability_topic,
            }
            self.enqueue(switch_disc_topic, json.dumps(switch_payload), retain=self.retain_discovery)
            
            # 4. Binary Sensor (Active status)
            binary_disc_topic = f"{self.discovery_prefix}/binary_sensor/hems_load_{load_id}_active/config"
            binary_payload = {
                "unique_id": f"hems_load_{load_id}_active",
                "name": "Active",
                "state_topic": f"{self.topic_prefix}/loads/{load_id}/active",
                "payload_on": "ON",
                "payload_off": "OFF",
                "icon": "mdi:circle-slice-8",
                "device": device_payload,
                "availability_topic": availability_topic,
            }
            self.enqueue(binary_disc_topic, json.dumps(binary_payload), retain=self.retain_discovery)

    async def publish_forecast_loads_state(self, gateway_id: str) -> None:
        """
        Compute and publish the state of each Forecast Load to Home Assistant via MQTT.
        """
        from src.services import db
        from src.services.smart_dispatch import get_site_season, is_schedule_active
        from src.routes.api_ha import get_ha_state
        import datetime
        
        try:
            loads = await db.get_all_forecast_loads()
        except Exception as e:
            logger.error(f"Failed to fetch forecast loads for state: {e}")
            return
            
        if not loads:
            return
            
        # Determine site season
        lat_val = await db.get_config_value(f"lat_{gateway_id}")
        if not lat_val:
            lat_val = await db.get_config_value("lat_global")
        
        current_season = "Summer"
        if lat_val:
            try:
                current_season = get_site_season(float(lat_val), datetime.datetime.now().month)
            except Exception:
                pass
                
        # Fetch live HA states for all loads with measurement_type == 'now'
        ha_states = {}
        for load in loads:
            if load.get("enabled", 1) and load.get("measurement_type") == "now":
                for key in ["ha_entity_id", "ha_switch_entity_id", "ha_binary_entity_id", "ha_energy_entity_id"]:
                    ent_id = load.get(key)
                    if ent_id:
                        try:
                            st = await get_ha_state(ent_id)
                            if st and "state" in st:
                                ha_states[ent_id] = st["state"]
                        except Exception as e:
                            logger.debug(f"Failed to fetch live HA state for {ent_id}: {e}")
                            
        # Process each load and publish state
        target_time = datetime.datetime.now()
        for load in loads:
            load_id = load["id"]
            enabled = bool(load.get("enabled", 1))
            m_type = load.get("measurement_type", "forecast")
            avg_kw = float(load.get("avg_kw", 0.0))
            
            # Filter loads by gateway_id if load has a specific gateway
            load_gw = load.get("gateway_id", "global")
            if load_gw != "global" and gateway_id != "global" and load_gw.upper() != gateway_id.upper():
                continue
                
            # Publish Switch State (Enabled)
            switch_topic = f"{self.topic_prefix}/loads/{load_id}/enabled"
            self.enqueue(switch_topic, "ON" if enabled else "OFF", qos=0, retain=True)
            
            # If disabled, power and binary active are both 0 / OFF
            if not enabled:
                self.enqueue(f"{self.topic_prefix}/loads/{load_id}/power_kw", "0.0", qos=0, retain=True)
                self.enqueue(f"{self.topic_prefix}/loads/{load_id}/active", "OFF", qos=0, retain=True)
                # Energy
                energy_val = 0.0
                energy_entity = load.get("ha_energy_entity_id")
                if energy_entity and energy_entity in ha_states:
                    try:
                        energy_val = float(ha_states[energy_entity])
                    except ValueError:
                        pass
                self.enqueue(f"{self.topic_prefix}/loads/{load_id}/energy_kwh", f"{energy_val:.2f}", qos=0, retain=True)
                continue
                
            # Calculate Power (kW)
            power_kw = 0.0
            is_active = False
            
            if m_type == "now":
                resolved = False
                # Try Power Sensor (ha_entity_id) first
                power_entity = load.get("ha_entity_id")
                if power_entity and power_entity in ha_states:
                    state = ha_states[power_entity]
                    try:
                        power_kw = float(state)
                        resolved = True
                        if power_kw > 0.0:
                            is_active = True
                    except ValueError:
                        if str(state).lower() in ("on", "true", "running", "active", "charging"):
                            power_kw = avg_kw
                            is_active = True
                            resolved = True
                            
                # Fallback to Switch Toggle
                if not resolved:
                    switch_entity = load.get("ha_switch_entity_id")
                    if switch_entity and switch_entity in ha_states:
                        state = ha_states[switch_entity]
                        if str(state).lower() in ("on", "true", "running", "active", "charging"):
                            power_kw = avg_kw
                            is_active = True
                            resolved = True
                            
                # Fallback to Binary Active
                if not resolved:
                    binary_entity = load.get("ha_binary_entity_id")
                    if binary_entity and binary_entity in ha_states:
                        state = ha_states[binary_entity]
                        if str(state).lower() in ("on", "true", "running", "active", "charging"):
                            power_kw = avg_kw
                            is_active = True
                            resolved = True
            else:
                # Schedule-based
                schedule_json = load.get("schedule_json", "[]")
                try:
                    periods = json.loads(schedule_json)
                except Exception:
                    periods = []
                
                for p in periods:
                    if is_schedule_active(p, target_time, current_season):
                        is_active = True
                        break
                
                if is_active:
                    power_kw = avg_kw
            
            # Resolve Daily Energy (kWh)
            energy_val = 0.0
            energy_entity = load.get("ha_energy_entity_id")
            if energy_entity and energy_entity in ha_states:
                try:
                    energy_val = float(ha_states[energy_entity])
                except ValueError:
                    pass
                    
            # Publish Power
            self.enqueue(f"{self.topic_prefix}/loads/{load_id}/power_kw", f"{power_kw:.3f}", qos=0, retain=True)
            
            # Publish Energy
            self.enqueue(f"{self.topic_prefix}/loads/{load_id}/energy_kwh", f"{energy_val:.2f}", qos=0, retain=True)
            
            # Publish Binary Active Status
            self.enqueue(f"{self.topic_prefix}/loads/{load_id}/active", "ON" if is_active else "OFF", qos=0, retain=True)
